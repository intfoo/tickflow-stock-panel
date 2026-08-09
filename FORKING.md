# Fork 分支模型与上游同步

本仓库是 [shy3130/tickflow-stock-panel](https://github.com/shy3130/tickflow-stock-panel) 的 fork。
本文件定义个人开发的分支规则，**所有 AI agent 提交代码前必须遵守**。

## 分支拓扑

```
upstream/main ────────────────  上游原仓库（只读镜像源）
main          = upstream/main   纯镜像，禁止直接提交任何个人修改
dev-main                      个人永久分支，所有个人修改最终汇入这里
feat/* fix/*                  短期功能分支，从 dev-main 切出，完成后合回
```

## 硬性规则

1. **禁止在 `main` 上提交**。main 只用于跟踪上游：`git checkout main && git pull`（本地 main 已 track `upstream/main`）。
2. **新工作一律从 `dev-main` 切功能分支**：`git checkout -b feat/xxx dev-main`。
3. **功能分支完成后合回 `dev-main`**，用 merge 不用 rebase（长期 fork 分支 rebase 会重复解同一个冲突）。
4. **提交优先**：切换分支、同步上游前，工作区不得有未提交的有价值改动。临时产物（`.codegraph/`、`pytest-out.txt` 等）不入库。
5. **私货隔离**：上游已有的文件（`docker.yml`、`release.yml`、`CONTRIBUTING.md` 等）保持与上游逐字节一致；fork 私有内容放独立新文件（如 `.github/workflows/deploy.yml`、本文件），实现零冲突同步。

## 同步上游（定期，建议每周）

```powershell
git checkout main; git pull                    # main 追上 upstream/main
git checkout dev-main; git merge main --no-edit # 合入 dev-main
# 有冲突解冲突后 git commit；lockfile 冲突见下
git push origin dev-main
```

## 冲突处理惯例

- **`backend/uv.lock` 冲突**：不手改。`git checkout <ours> -- backend/uv.lock` 后 `cd backend; uv lock` 按合并后的 pyproject 重新解析，再提交。
- **import 冲突**：两侧新增通常都要保留（如 useDialogBackdrop / useChartTheme 案例），确认文件体内均有引用后合并。
- **业务逻辑冲突**：以调用链和现有测试为准，解完必须跑相关测试验证。

## CI 说明

- `docker.yml` / `release.yml`：上游文件，不修改。
- `deploy.yml`（fork 私有，仅 dev-main）：push 到 dev-main 自动构建 `:dev` 镜像；配置 `vars.SERVER_HOST` + `secrets.SSH_KEY` 后自动 SSH 部署，未配置则自动跳过。
