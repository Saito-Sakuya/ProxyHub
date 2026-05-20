<p align="center">
  <h1 align="center">🌐 ProxyHub</h1>
  <p align="center">
    <b>统一代理池管理与智能路由系统</b><br/>
    <i>Unified Proxy Pool Manager & Smart Router</i>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python" />
    <img src="https://img.shields.io/badge/FastAPI-0.100+-green?logo=fastapi" />
    <img src="https://img.shields.io/badge/Mihomo-Core-orange" />
    <img src="https://img.shields.io/badge/Docker-Ready-blue?logo=docker" />
    <img src="https://img.shields.io/badge/License-MIT-yellow" />
  </p>
</p>

---

## ✨ 功能特性 / Features

- 🔄 **多订阅聚合** — 支持 Clash/V2Ray/Trojan 等多种格式订阅链接，同时支持直接粘贴节点文本
- 🌍 **按国家/地区自动分组** — 智能识别节点所属国家，自动分配独立端口池
- 🎯 **双重负载均衡策略**
  - `Rotate` (轮询) — 每个连接轮流使用不同节点，实现流量负载均衡
  - `Sticky` (粘性) — 基于一致性哈希，同一目标地址始终走同一节点，保持会话稳定
- 🔐 **VPS 安全防护** — 部署到服务器时自动启用 TOTP 双因子认证 + IP 封禁机制
- 📊 **实时 Web 仪表盘** — 精美 Glassmorphism 风格 UI，实时监控节点状态、流量、会话
- 🔑 **按国家独立凭据** — 不同国家/地区端口可设置不同的用户名和密码
- ⏱️ **自动定时同步** — 可配置订阅自动刷新间隔
- 🐳 **Docker 一键部署** — 提供 Dockerfile 和 docker-compose，开箱即用
- 💻 **Windows EXE 打包** — 支持 PyInstaller 编译为单文件可执行程序

---

## 🏗️ 系统架构 / Architecture

```
客户端 (Client)
    │
    ▼
┌─────────────────────────────────────────┐
│  SOCKS5 Smart Entry (Port 1080)         │  ← 统一智能入口
│  解析用户名 → 国家 + 策略 + 会话ID       │
│  凭据校验 (全局 / 按国家独立)             │
└─────────────┬───────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────┐
│  Mihomo/Clash Core (Ports 20000+)       │  ← 后端代理引擎
│                                         │
│  ┌─────────┐  ┌─────────┐              │
│  │ GLOBAL  │  │   HK    │  ...         │
│  │ Rotate  │  │ Rotate  │              │
│  │ 20000   │  │ 20002   │              │
│  │ Sticky  │  │ Sticky  │              │
│  │ 20001   │  │ 20003   │              │
│  └─────────┘  └─────────┘              │
│                                         │
│  负载均衡: round-robin / consistent-hash │
└─────────────────────────────────────────┘
              │
              ▼
         目标网站 (Target)
```

---

## 🚀 快速开始 / Quick Start

### 方式一：Docker 部署 (推荐)

```bash
# 1. 克隆仓库
git clone https://github.com/YOUR_USERNAME/ProxyHub.git
cd ProxyHub

# 2. 复制并编辑配置文件
cp config.json config.json  # 编辑其中的 subscriptions 订阅链接

# 3. 一键启动
docker compose up -d --build

# 4. 查看日志 (VPS 模式下获取 2FA 密钥)
docker logs proxyhub-service
```

### 方式二：直接运行 Python

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑配置文件
# 修改 config.json 中的 subscriptions 订阅链接

# 3. 启动服务
python main.py
```

### 方式三：Windows EXE 独立运行

```bash
# 编译为独立可执行文件
build_binary.bat

# 运行
dist/ProxyHub.exe
```

---

## 📡 端口说明 / Ports

| 端口 | 用途 | 说明 |
|------|------|------|
| `8000` | Web Dashboard | 管理面板 |
| `1080` | SOCKS5 Smart Entry | 统一代理智能入口 |
| `20000+` | Country Port Pool | 按国家分配的内部端口池 |

---

## 🔑 SOCKS5 用户名路由格式 / Username Routing

通过连接到智能入口 (1080 端口) 时设置的 SOCKS5 用户名，可以指定路由目标：

| 用户名格式 | 路由结果 | 说明 |
|------------|----------|------|
| *(空)* | `GLOBAL` + `rotate` | 全局轮询（默认） |
| `US` | `US` + `rotate` | 美国节点轮询 |
| `HK-sticky` | `HK` + `sticky` | 香港节点粘性会话 |
| `JP-sticky-sess123` | `JP` + `sticky` | 日本粘性 + 自定义会话ID |
| `rotate` | `GLOBAL` + `rotate` | 全局轮询 |

**如果启用了 SOCKS5 认证：**

用户名格式为 `[认证用户名]-[国家]-[策略]-[会话ID]`

例如：认证用户名为 `myuser`，则连接时使用 `myuser-US-rotate`

---

## ⚙️ 配置说明 / Configuration

首次启动前，请将 `config.json` 中的订阅链接替换为你自己的：

```json
{
  "subscriptions": [
    {
      "name": "我的订阅",
      "url": "https://your-provider.com/sub?target=clash",
      "enabled": true
    }
  ]
}
```

### 主要配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `smart_port` | `1080` | SOCKS5 智能入口监听端口 |
| `dashboard_port` | `8000` | Web 管理面板端口 |
| `port_pool_start` | `20000` | 国家端口池起始端口 |
| `socks5_auth.enabled` | `false` | 是否启用 SOCKS5 认证 |
| `sticky_session_ttl_minutes` | `30` | 粘性会话超时清理时间（分钟） |
| `auto_update_interval_hours` | `12` | 订阅自动刷新间隔（小时） |
| `alarm_threshold_percent` | `50` | 节点健康率告警阈值（%） |
| `dashboard_username` | `admin` | 面板登录用户名 |
| `dashboard_password` | `admin` | 面板登录密码 |

---

## 🔒 安全特性 / Security

### VPS 模式 (Linux)
- ✅ 自动启用 **TOTP 双因子认证**（Google Authenticator / Microsoft Authenticator）
- ✅ 首次启动时在终端日志中输出 2FA 密钥和 otpauth:// URI
- ✅ 所有 API 接口需携带有效 Token
- ✅ 登录失败 5 次自动 **IP 封禁 15 分钟**

### 本地模式 (Windows)
- 开发调试用，跳过认证直接访问面板

---

## 🛠️ 技术栈 / Tech Stack

- **后端**: Python 3.10+ / FastAPI / Uvicorn / asyncio
- **代理引擎**: [Mihomo](https://github.com/MetaCubeX/mihomo) (Clash Meta Fork)
- **前端**: 原生 HTML/CSS/JS，Glassmorphism UI 设计
- **容器化**: Docker / Docker Compose
- **打包**: PyInstaller (Windows EXE)

---

## 📁 项目结构 / Project Structure

```
ProxyHub/
├── main.py                 # FastAPI 主应用 & API 路由
├── core_manager.py         # Mihomo 核心管理（下载/配置生成/启停）
├── smart_proxy.py          # SOCKS5 智能入口代理服务
├── parse_sub.py            # 订阅链接解析（Clash/V2Ray/Trojan/文本）
├── config.json             # 运行时配置文件（不提交到 Git）
├── requirements.txt        # Python 依赖
├── Dockerfile              # Docker 镜像构建
├── docker-compose.yml      # Docker Compose 编排
├── build_binary.bat        # Windows EXE 编译脚本
├── proxyhub.spec           # PyInstaller 打包配置
└── web/                    # 前端 Web UI
    ├── index.html
    ├── css/style.css
    └── js/app.js
```

---

## 📜 License

[MIT License](LICENSE)

---

## ⚠️ 免责声明 / Disclaimer

本项目仅供学习与研究使用。用户需自行承担使用本软件的一切风险和法律责任。请遵守当地法律法规。

This project is for educational and research purposes only. Users are responsible for their own usage and must comply with local laws and regulations.
