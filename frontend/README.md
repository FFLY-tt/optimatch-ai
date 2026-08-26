# OptiMatch AI — 前端联调工具

给自己联调用的简单 React 界面，不追求生产级工程化（没有状态管理库/UI 组件库/路由）。

## 开发时必须同时跑两个服务

1. 先启动后端（项目根目录）：
   ```
   uvicorn src.main:app --reload
   ```
   （监听 http://localhost:8000）

2. 再启动前端（本目录）：
   ```
   npm run dev
   ```
   （监听 http://localhost:5173，`vite.config.js` 里配了 `/api` 和 `/files` 代理到 8000 端口的后端）

打开 http://localhost:5173 使用。**顺序很重要：后端没起来的话，前端所有接口调用都会失败。**
