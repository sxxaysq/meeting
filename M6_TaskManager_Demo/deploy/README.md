# 内网系统部署

访问链接：[会议任务管理系统](http://192.168.30.216/meeting/)

本分支仅提供结果查询、人工复核、组织机构和部门工作台。网页上传入口已停用，新材料使用 M1→M3→M6 批处理和展示库导出。

管理员页面的“部门工作台”提供全部 17 个部门入口。当前未启用账号登录与角色鉴权。

后端由 `meeting-taskmanager.service` 托管，开机启动、异常自动重启。现有 Nginx 的 `/meeting/` 路径转发到 `127.0.0.1:18098`；原门户路径保持不变。Vue 依赖本地提供，客户端不需要访问 CDN。

维护命令（216 服务器）：

```bash
sudo systemctl status meeting-taskmanager.service
sudo systemctl restart meeting-taskmanager.service
sudo journalctl -u meeting-taskmanager.service -n 50 --no-pager
curl -fsS http://127.0.0.1:18098/health
curl -fsS http://192.168.30.216/meeting/health
```

代码/数据库迁移前先 `systemctl stop`，完成后 `start`，不要只杀 PID。配置文件：

- `/etc/systemd/system/meeting-taskmanager.service`
- `/home/yty-s/meeting-m2-work/M6_TaskManager_Demo/deploy/service.env`（0600，不公开其内容）
- `/mnt/data/smartcore/frontend/nginx/main.conf`（frontend-main 容器 bind mount，仅新增 meeting 路由）

Nginx 配置使用文件 bind mount，宿主文件需原位写入以保留 inode，先备份和验证再 reload。不要重命名替换导致容器继续读取旧配置。

历史部署验证记录见 `deploy/release.json`。本分支的旧链路清理只修改仓库，尚未部署到服务器；部署后应重新检查健康接口、任务查询、人工复核和部门工作台。

备份目录位于 `/home/yty-s/meeting-m2-work/integration/m3_semantic_memory_20260917/backups/`：

- `lan_release_20260921_154414/`：数据库、原首页及部署回执。
- `lan_proxy_20260921_155144/`：原 Nginx 配置、原静态代码、原服务配置及部署回执。

Vue 来源和文件 SHA256、最终链接记录在 `deploy/release.json`。业务数据未因本次部署变更。
