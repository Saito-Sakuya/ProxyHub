import base64
import json
import re
import urllib.parse
import logging
import yaml
import aiohttp
import asyncio

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SubscriptionParser")

def decode_base64(data: str) -> str:
    """Helper to decode base64 strings with padding adjustment."""
    data = data.strip().replace("\r", "").replace("\n", "")
    # Add padding if necessary
    missing_padding = len(data) % 4
    if missing_padding:
        data += '=' * (4 - missing_padding)
    try:
        return base64.b64decode(data).decode('utf-8', errors='ignore')
    except Exception as e:
        logger.debug(f"Failed to decode base64 directly, trying urlsafe: {e}")
        try:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        except Exception as e2:
            logger.error(f"Base64 decoding failed: {e2}")
            return ""

def parse_ss(url_str: str) -> dict:
    """
    Parse shadowsocks (ss://) URL.
    Formats:
      ss://BASE64(method:password)@server:port#name
      ss://BASE64(method:password@server:port)#name
    """
    try:
        # Extract fragment (node name)
        parsed = urllib.parse.urlparse(url_str)
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else "Shadowsocks Node"
        
        netloc = parsed.netloc
        # If no netloc (due to format), use path/opaque
        if not netloc:
            netloc = parsed.path
            
        if "@" in netloc:
            # Format: ss://BASE64(method:password)@server:port
            auth_base64, server_port = netloc.split("@", 1)
            auth_decoded = decode_base64(auth_base64)
            if ":" not in auth_decoded:
                return {}
            method, password = auth_decoded.split(":", 1)
            if ":" in server_port:
                server, port = server_port.split(":", 1)
            else:
                server, port = server_port, "8388"
        else:
            # Format: ss://BASE64(method:password@server:port) or base64 unencoded method:password@server:port
            # Try to decode the entire netloc/path first
            decoded = decode_base64(netloc)
            if not decoded or "@" not in decoded:
                decoded = urllib.parse.unquote(netloc)
                
            if "@" in decoded:
                auth, server_port = decoded.split("@", 1)
                method, password = auth.split(":", 1)
                if ":" in server_port:
                    server, port = server_port.split(":", 1)
                else:
                    server, port = server_port, "8388"
            else:
                return {}

        # Strip any extraneous info in port (like query params)
        if "?" in port:
            port = port.split("?", 1)[0]

        return {
            "name": name.strip(),
            "type": "ss",
            "server": server.strip(),
            "port": int(port),
            "cipher": method.strip(),
            "password": password.strip(),
            "udp": True
        }
    except Exception as e:
        logger.error(f"Error parsing SS node: {e}")
        return {}

def parse_vmess(url_str: str) -> dict:
    """
    Parse vmess (vmess://) URL.
    Format: vmess://BASE64(JSON_STRING)
    """
    try:
        b64_content = url_str[8:]  # strip 'vmess://'
        json_str = decode_base64(b64_content)
        if not json_str:
            return {}
        
        config = json.loads(json_str)
        
        # Extract parameters
        name = config.get("ps", "VMess Node")
        server = config.get("add")
        port = config.get("port")
        uuid = config.get("id")
        alter_id = int(config.get("aid", 0))
        net = config.get("net", "tcp")
        
        if not server or not port or not uuid:
            return {}

        node = {
            "name": name.strip(),
            "type": "vmess",
            "server": server.strip(),
            "port": int(port),
            "uuid": uuid.strip(),
            "alterId": alter_id,
            "cipher": "auto",
            "udp": True
        }

        # TLS configuration
        tls_val = config.get("tls")
        if tls_val and str(tls_val).lower() in ["tls", "true", "1"]:
            node["tls"] = True
            if config.get("sni"):
                node["sni"] = config.get("sni")

        # Transport settings
        if net == "ws":
            node["network"] = "ws"
            ws_opts = {}
            if config.get("path"):
                ws_opts["path"] = config.get("path")
            if config.get("host"):
                ws_opts["headers"] = {"Host": config.get("host")}
            if ws_opts:
                node["ws-opts"] = ws_opts
        elif net == "grpc":
            node["network"] = "grpc"
            grpc_opts = {}
            if config.get("path"):
                grpc_opts["grpc-service-name"] = config.get("path")
            if grpc_opts:
                node["grpc-opts"] = grpc_opts
        elif net == "h2":
            node["network"] = "h2"
            h2_opts = {}
            if config.get("path"):
                h2_opts["path"] = config.get("path")
            if config.get("host"):
                h2_opts["host"] = [config.get("host")]
            if h2_opts:
                node["h2-opts"] = h2_opts

        return node
    except Exception as e:
        logger.error(f"Error parsing VMess node: {e}")
        return {}

def parse_vless_or_trojan(url_str: str, protocol_type: str) -> dict:
    """
    Parse vless:// or trojan:// URLs.
    Formats:
      vless://uuid@server:port?query#name
      trojan://password@server:port?query#name
    """
    try:
        parsed = urllib.parse.urlparse(url_str)
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else f"{protocol_type.upper()} Node"
        
        # Extract auth and host
        auth = parsed.username or parsed.userinfo
        server = parsed.hostname
        port = parsed.port

        if not auth or not server or not port:
            # Fallback for parsing issues when username has special characters
            netloc = parsed.netloc
            if "@" in netloc:
                auth_part, host_part = netloc.rsplit("@", 1)
                auth = auth_part
                if ":" in host_part:
                    server, port_str = host_part.split(":", 1)
                    port = int(port_str.split("?")[0])
                else:
                    server = host_part
                    port = 443

        if not auth or not server or not port:
            return {}

        node = {
            "name": name.strip(),
            "type": protocol_type,
            "server": server.strip(),
            "port": int(port),
            "udp": True
        }

        if protocol_type == "vless":
            node["uuid"] = auth.strip()
            node["cipher"] = "auto"
        else:
            node["password"] = auth.strip()

        # Parse query params
        queries = urllib.parse.parse_qs(parsed.query)
        
        # TLS config
        security = queries.get("security", [""])[0].lower()
        xtls = queries.get("xtls", [""])[0].lower()
        
        if security in ["tls", "xtls", "reality"] or xtls == "true" or parsed.port == 443:
            node["tls"] = True
            
        # SNI
        sni = queries.get("sni", [""])[0]
        if sni:
            node["sni"] = sni
            
        # Flow
        flow = queries.get("flow", [""])[0]
        if flow:
            node["flow"] = flow

        # Transport network
        net = queries.get("type", ["tcp"])[0].lower()
        if net == "ws":
            node["network"] = "ws"
            ws_opts = {}
            path = queries.get("path", [""])[0]
            if path:
                ws_opts["path"] = path
            host = queries.get("host", [""])[0]
            if host:
                ws_opts["headers"] = {"Host": host}
            if ws_opts:
                node["ws-opts"] = ws_opts
        elif net == "grpc":
            node["network"] = "grpc"
            grpc_opts = {}
            service_name = queries.get("serviceName", [""])[0]
            if service_name:
                grpc_opts["grpc-service-name"] = service_name
            if grpc_opts:
                node["grpc-opts"] = grpc_opts
        elif net == "h2":
            node["network"] = "h2"
            h2_opts = {}
            path = queries.get("path", [""])[0]
            if path:
                h2_opts["path"] = path
            host = queries.get("host", [""])[0]
            if host:
                h2_opts["host"] = [host]
            if h2_opts:
                node["h2-opts"] = h2_opts

        # Reality public key and short ID
        pbk = queries.get("pbk", [""])[0]
        sid = queries.get("sid", [""])[0]
        if pbk:
            # Mihomo Reality parameters
            node["reality-opts"] = {
                "public-key": pbk
            }
            if sid:
                node["reality-opts"]["short-id"] = sid

        return node
    except Exception as e:
        logger.error(f"Error parsing {protocol_type} node: {e}")
        return {}

def identify_country(node_name: str, aliases: dict) -> str:
    """
    Identify node's country code based on keywords in its name.
    Returns country code (e.g. 'US', 'HK') or 'Others' if unmatched.
    """
    node_name_lower = node_name.lower()
    
    # Try exact matches from config first
    for country, keywords in aliases.items():
        for keyword in keywords:
            # Avoid partial matching bugs (like "US" matching "BUS" or "SG" matching "PSG")
            # We check boundary or just standard lowercase substring
            kw_lower = keyword.lower()
            if kw_lower in node_name_lower:
                return country
                
    # Direct regex fallback for brackets or standalone codes like [HK] or -US-
    pattern = r'\b(HK|US|JP|SG|TW|KR|UK|DE|FR|CA|AU|RU|CN)\b'
    match = re.search(pattern, node_name.upper())
    if match:
        return match.group(1)
        
    return "Others"

def filter_node(node_name: str, filters: dict) -> bool:
    """
    Determine if a node should be included.
    Returns True to keep, False to filter out.
    """
    node_name_lower = node_name.lower()
    
    # Exclude check
    for exc in filters.get("exclude", []):
        if exc.lower() in node_name_lower:
            return False
            
    # Include check
    inc_list = filters.get("include", [])
    if inc_list:
        for inc in inc_list:
            if inc.lower() in node_name_lower:
                return True
        return False
        
    return True

async def fetch_and_parse_subscription(url: str, config: dict, sub_name: str = "Unknown") -> tuple:
    """
    Fetch raw subscription content (async) and parse all nodes.
    Supports base64 URI list subscriptions AND Clash YAML configs.
    Bypasses HTTP fetch if url starts with proxy schemes or contains newlines (raw text import).
    Returns (nodes_list, status_dict, extracted_metadata).
    """
    nodes = []
    extracted_metadata = {}
    logger.info(f"Processing subscription '{sub_name}'...")
    
    headers = {
        "User-Agent": "ClashMetaNode/1.0.0 mihomo/1.18.0"  # mimic Clash to get correct config
    }
    
    # Check if this is a raw copy-pasted list of node URIs instead of a URL
    is_raw_text = False
    url_trimmed = url.strip()
    if url_trimmed.startswith(("ss://", "vmess://", "vless://", "trojan://")) or "\n" in url_trimmed:
        is_raw_text = True
        
    if is_raw_text:
        logger.info(f"Subscription '{sub_name}' is raw URI list. Bypassing HTTP download.")
        content = url_trimmed
    else:
        logger.info(f"Fetching subscription '{sub_name}' from: {url}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        logger.error(f"Failed to fetch subscription {sub_name}. HTTP Status: {response.status}")
                        return [], {"name": sub_name, "status": "failure", "error": f"HTTP Status {response.status}"}, {}
                    content = await response.text()
        except Exception as e:
            logger.error(f"Error fetching subscription {sub_name} {url}: {e}")
            return [], {"name": sub_name, "status": "failure", "error": str(e)}, {}

    content = content.strip()
    if not content:
        return [], {"name": sub_name, "status": "success", "count": 0}, {}

    aliases = config.get("country_aliases", {})
    filters = config.get("filters", {})

    # Helper function to extract metadata from node name
    def check_and_extract_metadata(node_name: str) -> bool:
        is_meta = False
        
        # Expire pattern: e.g. "Expire: 2026-07-05"
        expire_match = re.search(r'(?:expire|到期|过期|有效期)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})', node_name, re.IGNORECASE)
        if expire_match:
            extracted_metadata["expire_date"] = expire_match.group(1)
            is_meta = True
            
        # Traffic pattern: e.g. "Traffic: 1.07 GB / 300 GB"
        traffic_match = re.search(r'(?:traffic|流量)\s*[:：]?\s*[\d\.]+\s*(?:GB|MB|TB)?\s*/\s*(\d+(?:\.\d+)?)\s*(GB|MB|TB)', node_name, re.IGNORECASE)
        if traffic_match:
            val = float(traffic_match.group(1))
            unit = traffic_match.group(2).upper()
            if unit == "MB":
                val = val / 1024.0
            elif unit == "TB":
                val = val * 1024.0
            extracted_metadata["total_traffic_gb"] = int(val)
            is_meta = True
            
        # Generic prefix checks
        node_name_lower = node_name.lower()
        if node_name_lower.startswith(("traffic:", "expire:", "流量:", "到期:", "过期:")):
            is_meta = True
            
        return is_meta

    # 1. Check if the content is a Clash YAML file
    if "proxies:" in content:
        try:
            clash_config = yaml.safe_load(content)
            clash_proxies = clash_config.get("proxies", [])
            if isinstance(clash_proxies, list):
                logger.info(f"Parsed {len(clash_proxies)} nodes from Clash YAML subscription.")
                
                processed_proxies = []
                for node in clash_proxies:
                    if node and node.get("name"):
                        node_name = node["name"]
                        
                        # Extract and skip metadata nodes
                        if check_and_extract_metadata(node_name):
                            logger.info(f"Auto-extracted metadata node: {node_name}")
                            continue
                            
                        if not filter_node(node_name, filters):
                            logger.debug(f"Filtered out Clash node: {node_name}")
                            continue
                        country = identify_country(node_name, aliases)
                        node["_country"] = country
                        processed_proxies.append(node)
                return processed_proxies, {"name": sub_name, "status": "success", "count": len(processed_proxies)}, extracted_metadata
        except Exception as e:
            logger.debug(f"Content contains 'proxies:' but failed to parse as YAML: {e}. Trying raw base64...")

    # 2. Try base64 decoding (standard V2Ray list)
    decoded = decode_base64(content)
    if not decoded or not ("ss://" in decoded or "vmess://" in decoded or "vless://" in decoded or "trojan://" in decoded):
        # Maybe it's already unencoded raw list of URLs
        decoded = content

    lines = decoded.splitlines()
    logger.info(f"Found {len(lines)} raw URI lines in subscription.")

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
            
        node = {}
        if line.startswith("ss://"):
            node = parse_ss(line)
        elif line.startswith("vmess://"):
            node = parse_vmess(line)
        elif line.startswith("vless://"):
            node = parse_vless_or_trojan(line, "vless")
        elif line.startswith("trojan://"):
            node = parse_vless_or_trojan(line, "trojan")
            
        if node and node.get("name") and node.get("server") and node.get("port"):
            node_name = node["name"]
            
            # Extract metadata from node name and discard metadata-only nodes
            if check_and_extract_metadata(node_name):
                logger.info(f"Auto-extracted metadata node: {node_name}")
                continue
                
            # Filtering
            if not filter_node(node_name, filters):
                logger.debug(f"Filtered out node: {node_name}")
                continue
                
            # Classify country
            country = identify_country(node_name, aliases)
            node["_country"] = country
            
            nodes.append(node)
            
    logger.info(f"Successfully parsed {len(nodes)} valid nodes.")
    return nodes, {"name": sub_name, "status": "success", "count": len(nodes)}, extracted_metadata

async def update_all_subscriptions(config: dict) -> tuple:
    """
    Fetch all subscriptions from config and merge them.
    Deduplicates nodes by name.
    Returns (all_nodes, sync_results_list).
    """
    all_nodes = []
    seen_names = set()
    sync_results = []
    
    tasks = []
    sub_names = []
    for sub in config.get("subscriptions", []):
        if sub.get("enabled", True):
            tasks.append(fetch_and_parse_subscription(sub["url"], config, sub["name"]))
            sub_names.append(sub["name"])
            
    if not tasks:
        logger.warning("No enabled subscriptions found in config.")
        return [], []
        
    results = await asyncio.gather(*tasks)
    
    # Overrides dictionary
    overrides = config.get("node_overrides", {})

    for nodes, status, extracted_meta in results:
        sync_results.append(status)
        
        # Merge auto-extracted metadata back to config!
        if extracted_meta:
            for sub in config.get("subscriptions", []):
                if sub["name"] == status["name"]:
                    if "expire_date" in extracted_meta:
                        sub["expire_date"] = extracted_meta["expire_date"]
                        logger.info(f"Saved auto-extracted expire date {extracted_meta['expire_date']} to {sub['name']}")
                    if "total_traffic_gb" in extracted_meta:
                        sub["total_traffic_gb"] = extracted_meta["total_traffic_gb"]
                        logger.info(f"Saved auto-extracted total traffic {extracted_meta['total_traffic_gb']} GB to {sub['name']}")
                        
        for node in nodes:
            name = node["name"]
            # Deduplicate name in Clash config
            base_name = name
            counter = 1
            while name in seen_names:
                name = f"{base_name} ({counter})"
                counter += 1
            node["name"] = name
            seen_names.add(name)
            
            # Apply manual overrides
            if name in overrides:
                node_over = overrides[name]
                if "country" in node_over:
                    node["_country"] = node_over["country"]
                if "enabled" in node_over:
                    node["_enabled"] = node_over["enabled"]
            else:
                node["_enabled"] = True
                
            all_nodes.append(node)
            
    logger.info(f"Merged subscriptions: total {len(all_nodes)} unique nodes.")
    return all_nodes, sync_results

# Quick standalone testing helper
if __name__ == "__main__":
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Test identification
    names = ["🇸🇬 新加坡 SG-IPL-01", "🇺🇸 美国 02-BGP", "香港 HK-BGP-CN2", "Tokyo JP 1", "Others node"]
    aliases = cfg.get("country_aliases", {})
    for n in names:
        print(f"Name: {n} -> Country: {identify_country(n, aliases)}")
