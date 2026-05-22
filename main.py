import os
import json
import asyncio
import logging
import urllib.parse
import aiohttp
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
from contextlib import asynccontextmanager

from parse_sub import update_all_subscriptions
from core_manager import CoreManager
from smart_proxy import SmartProxyServer

# Logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ProxyHubMain")

import sys
import base64
import hmac
import hashlib
import struct
import time
import secrets

IS_WINDOWS = sys.platform == "win32"
vps_session_tokens = set()
login_attempts = {}  # client_ip -> [failed_timestamps]
banned_ips = {}      # client_ip -> ban_expire_timestamp

def generate_base32_secret() -> str:
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "".join(secrets.choice(chars) for _ in range(16))

def verify_totp(secret: str, code: str) -> bool:
    try:
        secret = secret.strip().replace(" ", "").upper()
        missing_padding = len(secret) % 8
        if missing_padding:
            secret += "=" * (8 - missing_padding)
        key = base64.b32decode(secret)
    except Exception:
        return False

    try:
        code_int = int(code.strip())
    except ValueError:
        return False

    current_time = int(time.time())
    for drift in [-1, 0, 1]:
        counter = (current_time // 30) + drift
        counter_bytes = struct.pack(">Q", counter)
        hmac_res = hmac.new(key, counter_bytes, hashlib.sha1).digest()
        offset = hmac_res[-1] & 0x0F
        code_bytes = hmac_res[offset:offset+4]
        num = struct.unpack(">I", code_bytes)[0] & 0x7FFFFFFF
        calculated_code = num % 1000000
        if calculated_code == code_int:
            return True
    return False

# Global Application State
if getattr(sys, 'frozen', False):
    # Running inside compiled binary (PyInstaller)
    assets_dir = sys._MEIPASS
    workspace_dir = os.path.dirname(sys.executable)
else:
    # Running in standard Python development mode
    assets_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_dir = assets_dir

config_path = os.path.join(workspace_dir, "config.json")
config = {}
nodes_cache = []
core_manager = None
smart_proxy = None
sync_lock = asyncio.Lock()
is_syncing = False
last_sync_time = 0
last_sync_success = True
last_sync_results = []

def load_config() -> dict:
    """Helper to read config.json from disk."""
    global config
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            logger.error(f"config.json not found at: {config_path}")
            config = {}
    except Exception as e:
        logger.error(f"Error loading config.json: {e}")
        config = {}
        
    # Ensure default dashboard username and password exist
    changed = False
    if "dashboard_username" not in config:
        config["dashboard_username"] = "admin"
        changed = True
    if "dashboard_password" not in config:
        config["dashboard_password"] = "admin"
        changed = True
    if changed:
        save_config(config)
    return config

def save_config(new_config: dict) -> bool:
    """Helper to save new config.json back to disk."""
    global config
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(new_config, f, indent=2, ensure_ascii=False)
        config = new_config
        return True
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")
        return False

async def run_sync_logic():
    """Heavy lifting of fetching subscriptions and reloading Mihomo/SmartProxy."""
    global nodes_cache, is_syncing, last_sync_time, last_sync_success, last_sync_results
    async with sync_lock:
        is_syncing = True
        core_manager.add_log("[System] Triggering node subscription synchronization...")
        try:
            cfg = load_config()
            # Fetch nodes from subscriptions
            nodes, sync_results = await update_all_subscriptions(cfg)
            last_sync_results = sync_results
            
            # Save configuration back to disk (to store any dynamically auto-extracted metadata)
            save_config(cfg)
            
            # Print detailed logs about each subscription
            for res in sync_results:
                if res["status"] == "success":
                    core_manager.add_log(f"[System] Subscription '{res['name']}' successfully synced. Found {res['count']} nodes.")
                else:
                    core_manager.add_log(f"[ERROR] Subscription '{res['name']}' sync failed: {res['error']}")
            
            if not nodes:
                core_manager.add_log("[WARNING] No active nodes found during sync. Keeping current configuration.")
                last_sync_success = False
                return False
                
            nodes_cache = nodes
            
            # Restart Mihomo with new config
            core_manager.add_log(f"[System] Sync successful. Found {len(nodes)} active nodes. Restarting Mihomo core...")
            success = core_manager.restart(nodes)
            if success:
                core_manager.add_log("[System] Mihomo core restarted and listening on new ports.")
                last_sync_time = asyncio.get_event_loop().time()
                last_sync_success = True
                return True
            else:
                core_manager.add_log("[ERROR] Failed to start Mihomo with the new configuration.")
                last_sync_success = False
                return False
        except Exception as e:
            logger.error(f"Error syncing subscriptions: {e}")
            core_manager.add_log(f"[ERROR] Sync failed: {e}")
            last_sync_success = False
            return False
        finally:
            is_syncing = False

async def auto_sync_scheduler():
    """Background loop that periodically refreshes subscriptions."""
    while True:
        try:
            cfg = load_config()
            interval_hours = cfg.get("auto_update_interval_hours", 12)
            interval_seconds = interval_hours * 3600
            
            # Wait for next update
            await asyncio.sleep(interval_seconds)
            
            logger.info("Auto-sync scheduler triggered...")
            core_manager.add_log("[System] Scheduled subscription auto-refresh triggered.")
            await run_sync_logic()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in auto sync scheduler: {e}")
            await asyncio.sleep(60) # retry in a minute on error

# FastAPI Lifecycle Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup Initialization
    global core_manager, smart_proxy, nodes_cache, last_sync_success
    
    # 1. Load config
    cfg = load_config()
    
    # Load or generate 2FA secret on VPS
    if not IS_WINDOWS:
        changed = False
        secret = cfg.get("two_factor_secret", "")
        if not secret:
            secret = generate_base32_secret()
            cfg["two_factor_secret"] = secret
            changed = True
        
        # Make sure two_factor_enabled is True on VPS
        if not cfg.get("two_factor_enabled", True):
            cfg["two_factor_enabled"] = True
            changed = True
            
        if changed:
            save_config(cfg)
            
        # Print a beautiful ASCII Banner in VPS logs
        banner = f"""
========================================================================
🔒 PROXYHUB SECURITY INITIALIZATION (VPS MODE)
------------------------------------------------------------------------
2FA (Two-Factor Authentication) has been automatically enabled!
Because you are running on VPS, security authentication is required.

Your 2FA Secret Key: {secret}
Scan the QR code or manually enter the key in Authenticator:
otpauth://totp/ProxyHub?secret={secret}&issuer=ProxyHub

Please add this key to Google Authenticator or Microsoft Authenticator
immediately. You will need the 6-digit dynamic code to log in.
========================================================================
"""
        logger.info(banner)
    
    # 2. Instantiate managers
    core_manager = CoreManager(workspace_dir, cfg)
    smart_proxy = SmartProxyServer("0.0.0.0", cfg.get("smart_port", 1080), core_manager, cfg)
    
    # 3. Check / Download core binary
    core_downloaded = core_manager.check_and_download_core()
    if not core_downloaded:
        logger.error("Could not run proxy pool without Mihomo core.")
        # We still start the web server so user can download manually or fix settings
    else:
        # Perform initial sync and core start
        try:
            # We fetch from disk cache or subscription
            nodes_cache, _ = await update_all_subscriptions(cfg)
            if nodes_cache:
                core_manager.generate_config(nodes_cache)
                core_manager.start()
            else:
                core_manager.add_log("[System] Warning: No nodes loaded. Please add active subscriptions in the dashboard.")
                last_sync_success = False
        except Exception as e:
            logger.error(f"Failed initial subscription fetch: {e}")
            core_manager.add_log(f"[ERROR] Failed initial fetch: {e}")
            last_sync_success = False

    # 4. Start smart proxy SOCKS5 server
    await smart_proxy.start()
    
    # 5. Start background auto-sync task
    sync_task = asyncio.create_task(auto_sync_scheduler())
    
    yield
    
    # Shutdown / Clean up
    logger.info("Shutting down servers...")
    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass
        
    await smart_proxy.stop()
    core_manager.stop()

# Create FastAPI App
app = FastAPI(
    title="ProxyHub API",
    description="Unified Proxy Pool Manager and Dynamic Router",
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== AUTHENTICATION GATEWAY ====================

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if IS_WINDOWS:
        return await call_next(request)
        
    path = request.url.path
    # Allow static assets and frontend index.html/css/js without any token
    # Also allow auth routes themselves to prevent circular lockouts!
    if (
        path == "/" 
        or not path.startswith("/api") 
        or path in ["/api/auth/login", "/api/auth/verify", "/api/auth/info"]
    ):
        return await call_next(request)
        
    # Standard header checking: X-Access-Token or Authorization Bearer
    token = request.headers.get("X-Access-Token") or request.cookies.get("proxyhub_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            
    if not token or token not in vps_session_tokens:
        logger.warning(f"Unauthorized API request blocked: {path} (provided token: '{token}')")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "message": "未授权访问，请先登录！ (Unauthorized access, please login)"}
        )
        
    return await call_next(request)

class LoginReq(BaseModel):
    username: str
    password: str
    totp_code: str = ""

class VerifyReq(BaseModel):
    token: str

@app.get("/api/auth/info")
async def api_auth_info():
    return {
        "auth_required": not IS_WINDOWS,
        "two_factor_enabled": not IS_WINDOWS
    }

@app.post("/api/auth/login")
async def api_auth_login(req: LoginReq, request: Request):
    client_ip = request.client.host
    current_time = time.time()
    
    # 1. Check if IP is banned
    if client_ip in banned_ips:
        ban_expire = banned_ips[client_ip]
        if current_time < ban_expire:
            remaining_seconds = int(ban_expire - current_time)
            raise HTTPException(
                status_code=429,
                detail=f"由于连续登录失败次数过多，您的IP已被临时锁定！请在 {remaining_seconds} 秒后重试。"
            )
        else:
            # Ban expired
            del banned_ips[client_ip]
            if client_ip in login_attempts:
                del login_attempts[client_ip]
                
    # 2. Check environment
    if IS_WINDOWS:
        return {"status": "success", "token": "windows_bypass"}
        
    # 3. VPS Auth - Verify Username, Password, and 2FA code
    cfg = load_config()
    db_user = cfg.get("dashboard_username", "admin")
    db_pass = cfg.get("dashboard_password", "admin")
    secret = cfg.get("two_factor_secret", "")
    
    # Verify username, password and dynamic code
    if req.username.strip() == db_user and req.password == db_pass:
        if secret and verify_totp(secret, req.totp_code):
            # Success!
            # Clear IP failed attempts
            if client_ip in login_attempts:
                del login_attempts[client_ip]
                
            token = secrets.token_hex(24)
            vps_session_tokens.add(token)
            return {"status": "success", "token": token}
        
    # Fail!
    attempts = login_attempts.get(client_ip, [])
    # Filter out attempts older than 5 minutes (300s)
    attempts = [t for t in attempts if current_time - t < 300]
    attempts.append(current_time)
    login_attempts[client_ip] = attempts
    
    remaining = 5 - len(attempts)
    if remaining <= 0:
        banned_ips[client_ip] = current_time + 900  # Ban for 15 minutes
        raise HTTPException(
            status_code=429,
            detail="连续失败达到 5 次，您的IP已被锁定 15 分钟！"
        )
    else:
        raise HTTPException(
            status_code=401,
            detail=f"用户名、密码或动态验证码错误，请重新输入！您还剩 {remaining} 次尝试机会。"
        )

@app.post("/api/auth/verify")
async def api_auth_verify(req: VerifyReq):
    if IS_WINDOWS:
        return {"status": "success", "valid": True}
    if req.token in vps_session_tokens:
        return {"status": "success", "valid": True}
    return {"status": "error", "valid": False}

# ==================== API ROUTES ====================

async def check_mihomo_health() -> tuple:
    """
    Query Mihomo's proxy state to determine:
    (working_nodes_count, total_nodes_count, working_percentage)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:9090/proxies", timeout=1.5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    proxies = data.get("proxies", {})
                    
                    working = 0
                    total = 0
                    
                    # We only care about physical nodes, not proxy groups or load-balances
                    for name, p in proxies.items():
                        p_type = p.get("type", "").lower()
                        # Physical node types in Mihomo
                        if p_type in ["ss", "vmess", "vless", "trojan", "shadowsocks", "shadowsocksr"]:
                            total += 1
                            history = p.get("history", [])
                            if history:
                                last_delay = history[-1].get("delay", 0)
                                if last_delay > 0:
                                    working += 1
                                    
                    percent = (working / total * 100) if total > 0 else 100
                    return working, total, percent
    except Exception as e:
        logger.debug(f"Failed to query Mihomo proxies health: {e}")
    return 0, 0, 100

@app.get("/api/status")
async def get_status():
    """Retrieve system operating status metrics."""
    if not core_manager or not smart_proxy:
        raise HTTPException(status_code=503, detail="Service initializing")
        
    working_count, total_count, working_percent = await check_mihomo_health()
    
    # System alarm trigger rules:
    # 1. OK: subscription loaded successfully.
    # 2. Warning: subscription failed, but local cached nodes are active (>= alarm threshold).
    # 3. Alarm: subscription failed and active nodes are < threshold OR core down.
    cfg = load_config()
    threshold = cfg.get("alarm_threshold_percent", 50)
    
    system_health = "ok"
    if not core_manager.is_running:
        system_health = "alarm"
    elif not last_sync_success:
        if working_percent >= threshold:
            system_health = "warning"
        else:
            system_health = "alarm"
            
    return {
        "mihomo_running": core_manager.is_running,
        "smart_proxy_connections": smart_proxy.active_connections,
        "active_sessions_count": len(smart_proxy.active_sessions),
        "total_nodes": len(nodes_cache),
        "countries_count": len(core_manager.country_ports),
        "is_syncing": is_syncing,
        "last_sync": last_sync_time,
        "last_sync_success": last_sync_success,
        "last_sync_results": last_sync_results,
        "working_nodes": working_count,
        "working_percent": round(working_percent, 1),
        "system_health": system_health,
        "alarm_threshold": threshold,
        "group_traffic": smart_proxy.group_traffic
    }

@app.get("/api/config")
async def get_config():
    """Retrieve system config.json."""
    return load_config()

@app.post("/api/config")
async def post_config(new_config: dict):
    """Save new system config.json."""
    if save_config(new_config):
        return {"status": "success", "message": "Config saved successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save configuration")

@app.post("/api/sync")
async def post_sync(background_tasks: BackgroundTasks):
    """Trigger manual subscription synchronization and core reload."""
    global is_syncing
    if is_syncing:
        return {"status": "error", "message": "Sync is already in progress"}
        
    # Run sync in background task to respond immediately
    background_tasks.add_task(run_sync_logic)
    return {"status": "success", "message": "Sync triggered successfully"}

class NodeOverrideReq(BaseModel):
    node_name: str
    country: str = None
    enabled: bool = None

async def rebuild_and_restart_mihomo():
    """Background helper to safely rebuild Clash config and restart process."""
    async with sync_lock:
        core_manager.add_log("[System] Node overrides changed. Rebuilding config and restarting Mihomo...")
        success = core_manager.restart(nodes_cache)
        if success:
            core_manager.add_log("[System] Mihomo restarted successfully after manual overrides.")
        else:
            core_manager.add_log("[ERROR] Failed to start Mihomo after manual overrides.")

@app.post("/api/node/override")
async def post_node_override(req: NodeOverrideReq, background_tasks: BackgroundTasks):
    """Save manual overrides for a node, apply to active memory cache, and hot-reload."""
    global nodes_cache
    cfg = load_config()
    
    overrides = cfg.setdefault("node_overrides", {})
    node_over = overrides.setdefault(req.node_name, {})
    
    if req.country is not None:
        node_over["country"] = req.country
    if req.enabled is not None:
        node_over["enabled"] = req.enabled
        
    if save_config(cfg):
        # Update current cache directly
        for node in nodes_cache:
            if node.get("name") == req.node_name:
                if req.country is not None:
                    node["_country"] = req.country
                if req.enabled is not None:
                    node["_enabled"] = req.enabled
                    
        # Trigger config rebuild and restart Mihomo quietly in background
        background_tasks.add_task(rebuild_and_restart_mihomo)
        return {"status": "success", "message": "Node overrides updated successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save node overrides configuration")

@app.get("/api/nodes")
async def get_nodes():
    """Get parsed node list grouped by country."""
    if not core_manager:
        return {}
        
    grouped = {}
    for node in nodes_cache:
        country = node.get("_country", "Others")
        if country not in grouped:
            grouped[country] = []
        # Return clean node info (no passwords/secrets in overview)
        grouped[country].append({
            "name": node.get("name"),
            "type": node.get("type"),
            "server": node.get("server"),
            "port": node.get("port"),
            "tls": node.get("tls", False),
            "enabled": node.get("_enabled", True),
            "country": country
        })
        
    # Annotate with assigned ports
    ports_map = core_manager.country_ports
    result = {}
    for country, items in grouped.items():
        result[country] = {
            "nodes": items,
            "ports": ports_map.get(country, {"rotate": None, "sticky": None})
        }
    return result

@app.get("/api/logs")
async def get_logs():
    """Fetch recent log history."""
    if not core_manager:
        return []
    # Read up to recent 150 log lines
    return core_manager.get_log_lines(count=150)

class PortCredentialsReq(BaseModel):
    credentials: dict

class SystemSettingsReq(BaseModel):
    smart_port: int
    port_pool_start: int
    socks5_auth_enabled: bool
    socks5_auth_username: str = ""
    socks5_auth_password: str
    sticky_session_ttl_minutes: int
    alarm_threshold_percent: int
    dashboard_username: str = "admin"
    dashboard_password: str = "admin"

import re
URL_SAFE_PATTERN = re.compile(r'^[A-Za-z0-9\-_.]*$')

def validate_credential_safe(value: str, field_name: str):
    """Reject credentials containing URL-unsafe characters like @ # ! * % : / ?"""
    if value and not URL_SAFE_PATTERN.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} 包含不安全字符！仅允许字母、数字、连字符(-)、下划线(_)、点(.)。特殊字符会导致代理客户端 URL 解析失败。"
        )

@app.post("/api/system/settings")
async def post_system_settings(req: SystemSettingsReq, background_tasks: BackgroundTasks):
    """Update general system and smart proxy settings, auto-triggering necessary core restarts."""
    global smart_proxy, core_manager
    
    # Validate SOCKS5 credentials for URL safety
    if req.socks5_auth_enabled:
        validate_credential_safe(req.socks5_auth_username, "SOCKS5 用户名")
        validate_credential_safe(req.socks5_auth_password, "SOCKS5 密码")
    
    cfg = load_config()
    
    old_smart_port = cfg.get("smart_port", 1080)
    old_port_pool_start = cfg.get("port_pool_start", 20000)
    
    old_socks = cfg.get("socks5_auth", {})
    socks_changed = (
        req.socks5_auth_enabled != old_socks.get("enabled", False)
        or req.socks5_auth_username != old_socks.get("username", "")
        or req.socks5_auth_password != old_socks.get("password", "")
    )
    
    cfg["smart_port"] = req.smart_port
    cfg["port_pool_start"] = req.port_pool_start
    cfg["socks5_auth"] = {
        "enabled": req.socks5_auth_enabled,
        "username": req.socks5_auth_username,
        "password": req.socks5_auth_password
    }
    cfg["sticky_session_ttl_minutes"] = req.sticky_session_ttl_minutes
    cfg["alarm_threshold_percent"] = req.alarm_threshold_percent
    cfg["dashboard_username"] = req.dashboard_username
    cfg["dashboard_password"] = req.dashboard_password
    
    if not save_config(cfg):
        raise HTTPException(status_code=500, detail="Failed to save configuration to config.json")
        
    # Update smart proxy settings dynamically
    smart_proxy.config = cfg
    core_manager.port_pool_start = req.port_pool_start
    core_manager.config = cfg
    
    # If SOCKS5 smart port changed, we must restart SOCKS5 listener!
    if req.smart_port != old_smart_port:
        try:
            logger.info(f"SOCKS5 port changed from {old_smart_port} to {req.smart_port}. Restarting SOCKS5 listener...")
            core_manager.add_log(f"[System] SOCKS5 Entry port changed to {req.smart_port}. Re-binding listener...")
            await smart_proxy.stop()
            smart_proxy.port = req.smart_port
            await smart_proxy.start()
        except Exception as e:
            logger.error(f"Failed to restart SOCKS5 smart proxy on new port {req.smart_port}: {e}")
            core_manager.add_log(f"[ERROR] Failed to bind SOCKS5 to port {req.smart_port}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to bind SOCKS5 to new port: {e}")
            
    # If Clash port pool start or SOCKS5 auth settings changed, we need a complete rebuild and restart of Mihomo!
    if req.port_pool_start != old_port_pool_start or socks_changed:
        logger.info("Clash start port or SOCKS5 auth changed. Rebuilding config and restarting Mihomo.")
        background_tasks.add_task(rebuild_and_restart_mihomo)
        
    return {"status": "success", "message": "System settings updated successfully."}

@app.post("/api/port/credentials")
async def post_port_credentials(req: PortCredentialsReq, background_tasks: BackgroundTasks):
    """Save custom credentials for each country's static ports and rebuild config."""
    # Validate all credentials for URL safety
    for country, creds in req.credentials.items():
        if isinstance(creds, dict):
            validate_credential_safe(creds.get("username", ""), f"国家 {country} 的用户名")
            validate_credential_safe(creds.get("password", ""), f"国家 {country} 的密码")
    
    cfg = load_config()
    cfg["port_credentials"] = req.credentials
    if not save_config(cfg):
        raise HTTPException(status_code=500, detail="Failed to save port credentials configuration")
    
    # Dynamically update the config in the core manager
    core_manager.config = cfg
    
    # Rebuild Clash config and restart Mihomo core asynchronously in background
    logger.info("Port credentials changed. Rebuilding config and restarting Mihomo.")
    background_tasks.add_task(rebuild_and_restart_mihomo)
    
    return {"status": "success", "message": "地区中转端口凭据保存成功！"}

@app.post("/api/nodes/ping")
async def ping_nodes(payload: dict = None):
    """
    Test latency of a specific node or all nodes concurrently.
    Queries the Mihomo API: GET http://127.0.0.1:9090/proxies/{name}/delay
    """
    node_name = payload.get("node_name") if payload else None
    url = "http://www.gstatic.com/generate_204"
    timeout = 3000
    
    async def check_delay(name: str):
        encoded_name = urllib.parse.quote(name)
        check_url = f"http://127.0.0.1:9090/proxies/{encoded_name}/delay?timeout={timeout}&url={urllib.parse.quote(url)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(check_url, timeout=3.5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("delay", -1)
        except Exception as e:
            logger.debug(f"Failed to check delay for {name}: {e}")
        return -1

    if node_name:
        delay = await check_delay(node_name)
        return {"status": "success", "delays": {node_name: delay}}
    
    # Check all active nodes concurrently
    active_names = [n.get("name") for n in nodes_cache if n.get("_enabled", True) is not False]
    if not active_names:
        return {"status": "success", "delays": {}}
        
    tasks = [check_delay(name) for name in active_names]
    results = await asyncio.gather(*tasks)
    
    delays = {name: delay for name, delay in zip(active_names, results)}
    return {"status": "success", "delays": delays}

@app.get("/api/sessions")
async def get_sessions():
    """Get currently active SOCKS5 sticky sessions."""
    if not smart_proxy:
        return []
    return list(smart_proxy.active_sessions.keys())

# ==================== STATIC WEB UI MOUNT ====================

# Create web static directories if not exist to prevent FastAPI mount crash (only in dev mode)
web_dir = os.path.join(assets_dir, "web")
if not getattr(sys, 'frozen', False):
    os.makedirs(web_dir, exist_ok=True)
    os.makedirs(os.path.join(web_dir, "css"), exist_ok=True)
    os.makedirs(os.path.join(web_dir, "js"), exist_ok=True)

# Mount the static files folder AFTER API routes are defined so they aren't shadowed
# Serve static index.html or fallback
@app.get("/")
async def serve_index():
    index_file = os.path.join(web_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "ProxyHub is running! Please write the web files to complete the Web UI dashboard."}

# Mount static directory
app.mount("/", StaticFiles(directory=web_dir), name="web")
def run():
    """Serve the FastAPI application using Uvicorn."""
    cfg = load_config()
    web_port = cfg.get("dashboard_port", 8000)
    logger.info(f"Starting ProxyHub Web dashboard on http://0.0.0.0:{web_port}")
    uvicorn.run(app, host="0.0.0.0", port=web_port)

if __name__ == "__main__":
    run()
