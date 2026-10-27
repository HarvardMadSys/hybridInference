# Batch 入口与权限：第一阶段（简化范围）

日期：2026-10-27。状态：产品规则已批准，工作区实现完成，未合并或部署。
代码基线：`dev` 的 `321e9a6c457dcc80d35977aaac472d54041204a8`。
取代此前更复杂的入口/权限设想在第一阶段的适用部分；
UI 创建批次属于后续阶段，不在此记录内。

## 三条规则

1. `BATCH_ENABLED` 决定是否提供 Batch。关闭时仪表盘不显示入口，直接访问
   `/dashboard/batches` 显示"不可用"；数据与写接口仍按原有行为返回 404。管理员的
   批次总览/取消与此无关。
2. 现有 `BATCH_MIN_ROLE`（默认 `internal`）决定谁能提交新批次和修改 webhook。不新增
   设置、角色、权限管理界面或通用能力框架。
3. 低于该角色但有历史（未删除的自己的批次，或已保存的 webhook）的用户保留入口，可以
   查看、下载、取消、删除自己的批次，查看和删除 webhook；没有历史则不显示入口。页面
   明确说明不能提交新批次。

配额、API 密钥是否存在或轮换都不影响入口与历史管理；本阶段不读取密钥或花费。

## 实现

- `GET /user/batch-capabilities`（会话认证，`Cache-Control: no-store`）返回
  `user_id`、`status`（`enabled` / `disabled` / `unavailable`）、`enabled`、`min_role`、
  `role_may_submit`（仅角色，不含配额）、`has_history`、`history_error`、`show_entry`。
  关闭时返回 200 且不读取任何批次数据。
- 历史：`BatchStore.has_batches`（`EXISTS`，走 `idx_batch_jobs_user`）与
  `BatchWebhookStore.has_webhook`（主键 `EXISTS`，不读 URL/密钥），各自独立处理失败；
  任一为真即有历史，否则任一失败则为 `null`，入口保留。无新表、无新索引。
- 前端：`useBatchCapabilities` 按账号缓存，丢弃 `user_id` 不符的应答，刷新失败时保留
  该账号上次结果；聚焦时及页面可见时每分钟刷新。仪表盘分为 User Tools（Agents：internal
  + 功能开关；Playground：internal；Batches：`show_entry`）与 Admin Tools（仅 admin）。
- 首次查询失败且没有本账号结果时，internal/admin 沿用原入口；已知关闭结果优先，
  不因刷新失败重新开放。错账号响应按查询失败处理，不让页面无限加载。
- 用户角色沿用现有账户缓存；Admin API 修改角色会清除缓存。直接修改数据库的角色
  可能要等待原有60秒缓存失效，这不属于控制台配置流程。
- 后端所有者路由不检查角色，跨用户访问仍为 404；前端不以角色代替后端检查。
- 本阶段没有可点击的创建按钮；提交仍通过 API。
