"""企业微信智能机器人长连接服务 — WebSocket 保活。

与「群推送 Webhook」(webhook_adapter, 单向 POST) 并存的第二条企业微信通道:
智能机器人开「API 模式 / 长连接」后, 通过 WebSocket 双向通信, 支持 @机器人
交互、流式回复、模板卡片。负责连接保活(连接/鉴权/心跳/重连) + 会话注册 + aibot_send_msg 主动推送。

架构(对齐 depth_service):
  - daemon 线程内跑 asyncio 事件循环, 用已安装的 websockets(v16) 库
  - _running 线程存活标志 / _enabled 功能开关(持久化)
  - 指数退避重连 min(base * 2^(n-1), 60s), 与 useQuoteStream / _post_feishu 一致
  - 失败静默降级: 连接失败/凭证错误只记 WARNING, 不阻断应用启动

凭证来源: preferences.wecom_bot_id / wecom_bot_secret
连接地址: wss://openws.work.weixin.qq.com (官方固定)
协议帧(官方文档 path/101463):
  订阅 aibot_subscribe → 收 errcode=0 → 每 30s ping → 收消息/事件回调
限制: 每机器人仅 1 条长连接(新连接踢旧连接), 故配置变更需 stop→start
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid

logger = logging.getLogger(__name__)

# 企业微信智能机器人 WebSocket 固定连接地址
_WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
# 心跳间隔(官方要求 ≤30s, 否则服务端断开)
_HEARTBEAT_INTERVAL = 30.0
# 重连退避: base * 2^(n-1), 上限 60s (与 useQuoteStream.ts 一致)
_RECONNECT_BASE_DELAY = 5.0
_RECONNECT_CAP = 60.0
_RECONNECT_MAX_ATTEMPTS = 10  # 连续失败到此次数后仍继续重连, 仅放慢节奏


class WecomBotService:
    """企业微信智能机器人 WebSocket 长连接管理器 — 单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False          # 连接线程存活标志
        self._thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._connected = False        # WebSocket 是否已连接并通过鉴权
        self._last_error: str = ""
        self._app_state = None         # 延迟注入, 避免循环导入
        self._ws = None                  # 当前活跃 WebSocket 连接 (仅 ws loop 线程写)
        self._pending: dict = {}         # req_id -> asyncio.Future (仅 ws loop 线程读写)

    # ================================================================
    # 生命周期
    # ================================================================

    def set_app_state(self, app_state) -> None:
        """注入 FastAPI app.state (目前未使用, 预留消息处理时访问 monitor/repo)。"""
        self._app_state = app_state

    def start(self) -> bool:
        """启动长连接线程。凭证不齐或已运行则跳过。返回是否真正启动。"""
        from app.services import preferences

        bot_id = preferences.get_wecom_bot_id()
        secret = preferences.get_wecom_bot_secret()
        if not bot_id or not secret:
            logger.info("智能机器人未启动: 缺少 BotID 或 Secret")
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = ""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("智能机器人长连接服务已启动 (bot_id=%s)", bot_id)
        return True

    def stop(self) -> None:
        """停止长连接线程。"""
        with self._lock:
            self._running = False
            loop = self._ws_loop
        # 唤醒可能在 recv/退避 sleep 中的事件循环, 促使其退出
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self._ws_loop = None
        self._connected = False
        logger.info("智能机器人长连接服务已停止")

    def boot_check(self) -> None:
        """启动时检查 preferences, 凭证齐全且 enabled 则自动连接。

        失败静默降级(记 WARNING), 不阻断应用启动。
        """
        from app.services import preferences

        try:
            if preferences.get_wecom_bot_enabled():
                self.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("智能机器人 boot_check 失败: %s", e)

    def apply_credential_change(self) -> None:
        """配置变更后重建连接。单连接限制: 必须 stop 再 start。

        保存新凭证 → stop 旧连接 → 若 enabled 且凭证齐全则 start 新连接。
        """
        was_running = self._running
        if was_running:
            self.stop()
        # 重新读取最新凭证判断是否应启动
        from app.services import preferences
        if preferences.get_wecom_bot_enabled() and self.start():
            logger.info("智能机器人凭证已更新, 重新连接")
        elif was_running:
            logger.info("智能机器人凭证已更新, 但当前未启用或凭证不齐, 停止连接")

    def status(self) -> dict:
        """返回连接状态(供 UI 展示)。"""
        from app.services import preferences
        return {
            "enabled": preferences.get_wecom_bot_enabled(),
            "running": self._running,
            "connected": self._connected,
            "bot_id_configured": bool(preferences.get_wecom_bot_id()),
            "secret_configured": bool(preferences.get_wecom_bot_secret()),
            "last_error": self._last_error,
        }

    # ================================================================
    # 连接线程
    # ================================================================

    def _run_loop(self) -> None:
        """daemon 线程入口: 创建 asyncio 事件循环并运行连接主循环。"""
        try:
            loop = asyncio.new_event_loop()
            with self._lock:
                self._ws_loop = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._connect_loop())
        except Exception as e:  # noqa: BLE001
            logger.warning("智能机器人连接线程异常: %s", e)
            self._last_error = str(e)
        finally:
            with self._lock:
                self._connected = False
                self._ws_loop = None
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _connect_loop(self) -> None:
        """主连接循环: 连接 → 鉴权 → 心跳保活 → 断开 → 退避重连。"""
        import websockets
        from app.services import preferences

        attempt = 0
        while self._running:
            bot_id = preferences.get_wecom_bot_id()
            secret = preferences.get_wecom_bot_secret()
            if not bot_id or not secret:
                # 凭证被清空, 等待重新配置
                self._last_error = "缺少 BotID 或 Secret"
                await self._sleep_interruptible(5.0)
                continue

            try:
                async with websockets.connect(
                    _WECOM_WS_URL,
                    ping_interval=None,   # 用业务层 ping, 不用协议层
                    close_timeout=5,
                ) as ws:
                    # 1. 发送订阅鉴权帧
                    req_id = str(uuid.uuid4())
                    subscribe_frame = {
                        "cmd": "aibot_subscribe",
                        "headers": {"req_id": req_id},
                        "body": {"bot_id": bot_id, "secret": secret},
                    }
                    await ws.send(json.dumps(subscribe_frame))

                    # 2. 等待鉴权响应(errcode=0 表示成功)
                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    resp = json.loads(resp_raw)
                    errcode = resp.get("errcode", resp.get("body", {}).get("errcode", -1))
                    if errcode != 0:
                        errmsg = resp.get("errmsg", resp.get("body", {}).get("errmsg", "未知错误"))
                        self._last_error = f"鉴权失败(errcode={errcode}): {errmsg}"
                        logger.warning("智能机器人鉴权失败: %s", self._last_error)
                        # 鉴权失败是凭证问题, 重连无益, 等待用户修正
                        await self._sleep_interruptible(30)
                        continue

                    # 连接成功
                    with self._lock:
                        self._connected = True
                    self._last_error = ""
                    self._ws = ws
                    attempt = 0
                    logger.info("智能机器人已连接 (bot_id=%s)", bot_id)

                    # 3. 心跳保活 + 接收循环
                    await self._maintain_connection(ws)

            except asyncio.TimeoutError:
                self._last_error = "鉴权响应超时"
                logger.warning("智能机器人鉴权超时")
            except Exception as e:  # noqa: BLE001 — 网络/断开, 可重连
                self._last_error = str(e)
                logger.warning("智能机器人连接异常: %s", e)
            finally:
                with self._lock:
                    self._connected = False
                self._ws = None
                # 连接断开: 让所有等待响应的发送协程立即失败, 不等超时
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("连接已断开"))
                self._pending.clear()

            # 4. 指数退避重连
            if not self._running:
                break
            attempt += 1
            delay = min(_RECONNECT_BASE_DELAY * (2 ** (attempt - 1)), _RECONNECT_CAP)
            logger.info("智能机器人 %ds 后重连(第 %d 次)", delay, attempt)
            await self._sleep_interruptible(delay)

    async def _maintain_connection(self, ws) -> None:
        """连接保持阶段: 每 30s 发 ping, 同时接收服务端推送。

        本阶段收到消息解析并记 INFO 日志(验证接收能力), 后续消息处理在此扩展。
        """
        while self._running:
            try:
                # 用 wait_for 同时实现"心跳定时"和"接收消息", 哪个先到都行
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_INTERVAL)
                    self._dispatch_frame(raw)
                    continue
                except asyncio.TimeoutError:
                    pass  # 接收超时 → 到了心跳时间
                # 发送业务层 ping 保活
                await ws.send(json.dumps({"cmd": "ping"}))
            except Exception as e:  # noqa: BLE001 — 连接断开, 抛给上层重连
                raise

    def _dispatch_frame(self, raw) -> None:
        """分发收到的帧: 响应帧 (无 cmd 且 req_id 命中 pending) resolve future, 其余走回调处理。

        pong 响应 (ping 不带 req_id) 不在 pending 中, 自然落入回调分支仅记日志。
        """
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.info("智能机器人收到非 JSON 消息: %s", str(raw)[:200])
            return
        if not isinstance(frame, dict):
            logger.info("智能机器人收到非 dict JSON 帧: %s", str(raw)[:200])
            return
        req_id = (frame.get("headers") or {}).get("req_id")
        if "cmd" not in frame and req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                fut.set_result(frame)
            return
        self._handle_incoming(frame)

    def _handle_incoming(self, frame: dict) -> None:
        """处理回调帧: 消息/事件记日志并注册会话。"""
        cmd = frame.get("cmd", "?")
        body = frame.get("body", {}) or {}
        if cmd == "aibot_msg_callback":
            userid = body.get("from", {}).get("userid", "?")
            chattype = body.get("chattype", "?")
            msgtype = body.get("msgtype", "?")
            content = body.get("text", {}).get("content") or body.get("content", "")
            logger.info("智能机器人收到用户消息 [%s/%s] %s: %s",
                        chattype, msgtype, userid, str(content)[:100])
            self._register_chat(body)
        elif cmd == "aibot_event_callback":
            eventtype = body.get("event", {}).get("eventtype", "?")
            logger.info("智能机器人收到事件回调: %s", eventtype)
            if eventtype != "disconnected_event":
                self._register_chat(body)
        else:
            logger.info("智能机器人收到帧 cmd=%s: %s", cmd, str(frame)[:200])

    @staticmethod
    def _register_chat(body: dict) -> None:
        """从回调帧 body 提取会话注册: 群聊取 chatid (chat_type=2), 单聊取 from.userid (chat_type=1)。"""
        try:
            from app.services import preferences
            chatid = (body.get("chatid") or "").strip()
            if chatid:
                preferences.register_wecom_bot_chat(chatid, 2)
                return
            userid = (body.get("from") or {}).get("userid", "").strip()
            if userid:
                preferences.register_wecom_bot_chat(userid, 1)
        except Exception as e:  # noqa: BLE001
            logger.debug("会话注册失败 (不影响消息处理): %s", e)

    async def _send_frame(self, frame: dict) -> dict:
        """在 ws loop 内发送帧并按 req_id 等待响应。ws 不可用/超时抛异常。"""
        ws = self._ws
        if ws is None:
            raise RuntimeError("WebSocket 不可用")
        req_id = frame["headers"]["req_id"]
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await ws.send(json.dumps(frame))
            return await asyncio.wait_for(fut, timeout=10)
        finally:
            self._pending.pop(req_id, None)

    def send_markdown(self, chatid: str, chat_type: int, content: str) -> bool:
        """主动推送 markdown 消息到指定会话 (同步、线程安全, 供 webhook 执行池线程调用)。

        未连接/超时/errcode 非 0 → 记 WARNING 返回 False, 不抛异常 (与 webhook_adapter 语义一致)。
        """
        if not chatid:
            return False
        with self._lock:
            loop = self._ws_loop
            connected = self._connected
        if not loop or not connected:
            logger.warning("智能机器人未连接, 推送跳过 (chatid=%s)", chatid)
            return False
        frame = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": str(uuid.uuid4())},
            "body": {
                "chatid": chatid,
                "chat_type": int(chat_type),
                "msgtype": "markdown",
                "markdown": {"content": content},
            },
        }
        try:
            future = asyncio.run_coroutine_threadsafe(self._send_frame(frame), loop)
            resp = future.result(timeout=15)
        except Exception as e:  # noqa: BLE001
            logger.warning("智能机器人推送失败 (chatid=%s): %s", chatid, e)
            return False
        errcode = resp.get("errcode", -1)
        if errcode != 0:
            logger.warning("智能机器人推送被拒 (chatid=%s, errcode=%s): %s",
                           chatid, errcode, resp.get("errmsg", ""))
            return False
        logger.info("智能机器人推送成功 (chatid=%s)", chatid)
        return True

    async def _sleep_interruptible(self, seconds: float) -> None:
        """可被 stop() 中断的 sleep(通过检查 _running)。"""
        waited = 0.0
        while self._running and waited < seconds:
            await asyncio.sleep(min(0.5, seconds - waited))
            waited += 0.5
