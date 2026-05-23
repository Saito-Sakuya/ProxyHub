import asyncio
import logging
import socket
import re

logger = logging.getLogger("SmartProxy")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

class SmartProxyServer:
    def __init__(self, host: str, port: int, core_manager, config: dict):
        self.host = host
        self.port = port
        self.core_manager = core_manager
        self.config = config
        self.server = None
        self.active_connections = 0
        self.active_sessions = {}
        self.cleaner_task = None
        self.group_traffic = {} # { "US": { "rx": 0, "tx": 0 }, ... }
        
    async def start(self):
        """Start the async TCP server for SOCKS5."""
        self.server = await asyncio.start_server(
            self.handle_connection, self.host, self.port
        )
        logger.info(f"Smart Proxy Server listening on SOCKS5://{self.host}:{self.port}")
        self.core_manager.add_log(f"[SmartProxy] Smart Proxy listening on SOCKS5://{self.host}:{self.port}")
        
        # Start background TTL session cleaner
        self.cleaner_task = asyncio.create_task(self.session_ttl_cleaner())
        
    async def stop(self):
        """Stop the SOCKS5 server."""
        if self.cleaner_task:
            self.cleaner_task.cancel()
            try:
                await self.cleaner_task
            except asyncio.CancelledError:
                pass
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("Smart Proxy Server stopped.")
            self.core_manager.add_log("[SmartProxy] Smart Proxy stopped.")

    async def session_ttl_cleaner(self):
        """Background loop to clean up expired sessions from dashboard."""
        while True:
            try:
                await asyncio.sleep(10)
                ttl_minutes = self.config.get("sticky_session_ttl_minutes", 30)
                if ttl_minutes <= 0:
                    continue
                    
                ttl_seconds = ttl_minutes * 60
                now = asyncio.get_event_loop().time()
                
                expired = []
                for sess_key, last_active in list(self.active_sessions.items()):
                    if now - last_active > ttl_seconds:
                        expired.append(sess_key)
                        
                for sess_key in expired:
                    logger.info(f"Session {sess_key} expired after {ttl_minutes} minutes of inactivity. Cleaning from dashboard.")
                    self.core_manager.add_log(f"[SmartProxy] Sticky Session {sess_key} expired (TTL timeout) and cleared.")
                    self.active_sessions.pop(sess_key, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in session TTL cleaner: {e}")

    def parse_username(self, username: str) -> tuple:
        """
        Parse username to extract country and strategy.
        Format: [country]-[strategy]-[session_id]
        Examples:
          - "US-rotate" -> ("US", "rotate", None)
          - "HK-sticky-sess123" -> ("HK", "sticky", "sess123")
          - "JP" -> ("JP", "rotate", None)
          - "rotate" -> ("GLOBAL", "rotate", None)
        """
        country = "GLOBAL"
        strategy = "rotate"
        session_id = None

        if not username:
            return country, strategy, session_id

        parts = username.split("-")
        
        # Check first part (could be country or strategy)
        first = parts[0].upper()
        
        # If first part is just a strategy
        if first in ["ROTATE", "STICKY"]:
            strategy = first.lower()
            if len(parts) > 1:
                session_id = "-".join(parts[1:])
            return country, strategy, session_id

        # Look up if first part matches our known countries
        known_countries = list(self.core_manager.country_ports.keys())
        if first in known_countries:
            country = first
            if len(parts) > 1:
                second = parts[1].lower()
                if second in ["rotate", "sticky"]:
                    strategy = second
                    if len(parts) > 2:
                        session_id = "-".join(parts[2:])
                else:
                    # e.g., US-sess123 (defaults to rotate)
                    session_id = "-".join(parts[1:])
        else:
            # Country not matched in ports, default to GLOBAL or treat as country if it's 2 chars
            if len(first) == 2:
                country = first
            if len(parts) > 1:
                second = parts[1].lower()
                if second in ["rotate", "sticky"]:
                    strategy = second
                    if len(parts) > 2:
                        session_id = "-".join(parts[2:])
                else:
                    session_id = "-".join(parts[1:])

        return country, strategy, session_id

    async def handle_connection(self, client_reader, client_writer):
        """Handle incoming connection - auto-detect SOCKS5 vs HTTP proxy."""
        self.active_connections += 1
        peer = client_writer.get_extra_info('peername')
        logger.debug(f"New connection from {peer}")

        try:
            # Read first byte to detect protocol
            first_byte = await client_reader.readexactly(1)
            
            if first_byte[0] == 5:
                # SOCKS5 protocol
                await self._handle_socks5(client_reader, client_writer, first_byte)
            elif first_byte[0] in (ord('C'), ord('G'), ord('P'), ord('H'), ord('D'), ord('O'), ord('T')):
                # HTTP method detected (CONNECT, GET, POST, HEAD, DELETE, OPTIONS, TRACE/PUT)
                await self._handle_http_proxy(client_reader, client_writer, first_byte)
            else:
                logger.warning(f"Unsupported protocol, first byte: {first_byte[0]}")
                client_writer.close()
                return
        except asyncio.CancelledError:
            pass
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            logger.error(f"Error in connection handler: {e}")
        finally:
            self.active_connections -= 1
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except:
                pass

    # ==================== HTTP PROXY HANDLER ====================

    async def _handle_http_proxy(self, client_reader, client_writer, first_byte):
        """Handle HTTP proxy requests (CONNECT tunnel & plain HTTP forward)."""
        # Read the rest of the first line
        rest_of_line = await client_reader.readline()
        first_line = (first_byte + rest_of_line).decode('utf-8', errors='ignore').strip()
        
        # Parse: METHOD target HTTP/1.x
        parts = first_line.split(' ')
        if len(parts) < 3:
            client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await client_writer.drain()
            return
        
        method = parts[0].upper()
        target = parts[1]
        http_version = parts[2]
        
        # Read all headers
        headers = {}
        raw_headers = b""
        while True:
            line = await client_reader.readline()
            raw_headers += line
            line_str = line.decode('utf-8', errors='ignore').strip()
            if not line_str:
                break
            if ':' in line_str:
                key, val = line_str.split(':', 1)
                headers[key.strip().lower()] = val.strip()
        
        # Extract auth from Proxy-Authorization header
        username = None
        auth_config = self.config.get("socks5_auth", {})
        auth_enabled = auth_config.get("enabled", False)
        auth_username = auth_config.get("username", "")
        auth_password = auth_config.get("password", "anypassword")
        
        if auth_enabled:
            proxy_auth = headers.get('proxy-authorization', '')
            if not proxy_auth:
                client_writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"ProxyHub\"\r\n\r\n")
                await client_writer.drain()
                return
            
            try:
                import base64
                scheme, cred_b64 = proxy_auth.split(' ', 1)
                cred = base64.b64decode(cred_b64).decode('utf-8', errors='ignore')
                cred_user, cred_pass = cred.split(':', 1)
            except Exception:
                client_writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"ProxyHub\"\r\n\r\n")
                await client_writer.drain()
                return
            
            # Determine country from username for per-country credentials
            requested_country = "GLOBAL"
            match = re.search(r'(?:^|-)(GLOBAL|Others|[A-Z]{2})-(rotate|sticky)(?:-|$)', cred_user, re.IGNORECASE)
            if match:
                requested_country = match.group(1).upper()
            
            port_credentials = self.config.get("port_credentials", {})
            c_creds = port_credentials.get(requested_country, {})
            c_u = c_creds.get("username", "").strip()
            c_p = c_creds.get("password", "")
            
            expected_user_prefix = c_u if c_u else auth_username
            expected_password = c_p if c_p else auth_password
            
            if cred_pass != expected_password:
                logger.warning(f"HTTP Proxy auth failed for user {cred_user}: password mismatch.")
                client_writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"ProxyHub\"\r\n\r\n")
                await client_writer.drain()
                return
            
            # Verify username prefix and extract routing info
            if expected_user_prefix:
                if cred_user == expected_user_prefix:
                    username = ""
                elif cred_user.startswith(expected_user_prefix + "-"):
                    username = cred_user[len(expected_user_prefix) + 1:]
                else:
                    logger.warning(f"HTTP Proxy auth failed: username '{cred_user}' does not match prefix '{expected_user_prefix}'.")
                    client_writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"ProxyHub\"\r\n\r\n")
                    await client_writer.drain()
                    return
            else:
                username = cred_user
        else:
            # No auth - try to extract routing from Proxy-Authorization if provided, else default
            proxy_auth = headers.get('proxy-authorization', '')
            if proxy_auth:
                try:
                    import base64
                    scheme, cred_b64 = proxy_auth.split(' ', 1)
                    cred = base64.b64decode(cred_b64).decode('utf-8', errors='ignore')
                    username = cred.split(':', 1)[0]
                except:
                    username = ""
            else:
                username = ""
        
        # Parse routing from username
        country, strategy, session_id = self.parse_username(username)
        
        # Session tracking
        session_key = f"{country}-{strategy}"
        if session_id:
            session_key += f"-{session_id}"
        self.active_sessions[session_key] = asyncio.get_event_loop().time()
        
        # Get target Mihomo port
        target_port = self.get_target_clash_port(country, strategy)
        
        if method == 'CONNECT':
            await self._handle_http_connect(client_reader, client_writer, target, target_port, session_key, country)
        else:
            await self._handle_http_forward(client_reader, client_writer, first_line, raw_headers, headers, target_port, session_key, country)
    
    async def _handle_http_connect(self, client_reader, client_writer, target, target_port, session_key, country):
        """Handle HTTP CONNECT tunneling via Mihomo SOCKS5."""
        # Parse host:port from target
        if ':' in target:
            dest_host, dest_port_str = target.rsplit(':', 1)
            try:
                dest_port = int(dest_port_str)
            except ValueError:
                dest_port = 443
        else:
            dest_host = target
            dest_port = 443
        
        logger.debug(f"HTTP CONNECT tunnel: {dest_host}:{dest_port} via Mihomo port {target_port}")
        
        # Connect to Mihomo mixed port via SOCKS5
        try:
            clash_reader, clash_writer = await asyncio.open_connection('127.0.0.1', target_port)
        except Exception as e:
            logger.error(f"Failed to connect to Mihomo port {target_port}: {e}")
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            return
        
        try:
            # SOCKS5 handshake with Mihomo (no auth)
            clash_writer.write(bytes([5, 1, 0]))
            await clash_writer.drain()
            
            resp = await clash_reader.readexactly(2)
            if resp[0] != 5 or resp[1] != 0:
                raise Exception("Mihomo rejected SOCKS5 handshake")
            
            # SOCKS5 CONNECT request
            # atyp=3 (domain), addr, port
            addr_bytes = dest_host.encode('utf-8')
            clash_writer.write(bytes([5, 1, 0, 3, len(addr_bytes)]) + addr_bytes + dest_port.to_bytes(2, 'big'))
            await clash_writer.drain()
            
            conn_resp = await clash_reader.readexactly(4)
            if conn_resp[1] != 0:
                raise Exception(f"Mihomo CONNECT failed with status {conn_resp[1]}")
            
            # Read remaining address data from response
            c_atyp = conn_resp[3]
            if c_atyp == 1:
                await clash_reader.readexactly(6)
            elif c_atyp == 3:
                c_len = (await clash_reader.readexactly(1))[0]
                await clash_reader.readexactly(c_len + 2)
            elif c_atyp == 4:
                await clash_reader.readexactly(18)
            
        except Exception as e:
            logger.error(f"Error tunneling via Mihomo: {e}")
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            try:
                clash_writer.close()
            except:
                pass
            return
        
        # Success - tell client tunnel is established
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()
        
        # Bidirectional pipe
        await asyncio.gather(
            self.pipe(client_reader, clash_writer, session_key, country, "tx"),
            self.pipe(clash_reader, client_writer, session_key, country, "rx"),
            return_exceptions=True
        )
        self.active_sessions.pop(session_key, None)
    
    async def _handle_http_forward(self, client_reader, client_writer, first_line, raw_headers, headers, target_port, session_key, country):
        """Handle plain HTTP proxy forwarding (GET/POST etc) via SOCKS5 tunnel to Mihomo."""
        # Parse target host:port from the request URL (e.g. GET http://httpbin.org/ip HTTP/1.1)
        parts = first_line.split(' ')
        target_url = parts[1] if len(parts) > 1 else ''
        
        # Extract host and port from URL
        from urllib.parse import urlparse
        parsed = urlparse(target_url)
        dest_host = parsed.hostname or ''
        dest_port = parsed.port or 80
        
        if not dest_host:
            client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await client_writer.drain()
            return
        
        # Convert absolute URL to relative path for the forwarded request
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        
        logger.debug(f"HTTP forward: {dest_host}:{dest_port}{path} via Mihomo port {target_port}")
        
        # Connect to Mihomo via SOCKS5 tunnel (same approach as CONNECT)
        try:
            clash_reader, clash_writer = await asyncio.open_connection('127.0.0.1', target_port)
        except Exception as e:
            logger.error(f"Failed to connect to Mihomo port {target_port}: {e}")
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            return
        
        try:
            # SOCKS5 handshake with Mihomo
            clash_writer.write(bytes([5, 1, 0]))
            await clash_writer.drain()
            
            resp = await clash_reader.readexactly(2)
            if resp[0] != 5 or resp[1] != 0:
                raise Exception("Mihomo rejected SOCKS5 handshake")
            
            # SOCKS5 CONNECT to target
            addr_bytes = dest_host.encode('utf-8')
            clash_writer.write(bytes([5, 1, 0, 3, len(addr_bytes)]) + addr_bytes + dest_port.to_bytes(2, 'big'))
            await clash_writer.drain()
            
            conn_resp = await clash_reader.readexactly(4)
            if conn_resp[1] != 0:
                raise Exception(f"Mihomo CONNECT failed with status {conn_resp[1]}")
            
            # Read remaining address data
            c_atyp = conn_resp[3]
            if c_atyp == 1:
                await clash_reader.readexactly(6)
            elif c_atyp == 3:
                c_len = (await clash_reader.readexactly(1))[0]
                await clash_reader.readexactly(c_len + 2)
            elif c_atyp == 4:
                await clash_reader.readexactly(18)
                
        except Exception as e:
            logger.error(f"Error tunneling HTTP forward via Mihomo: {e}")
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            try:
                clash_writer.close()
            except:
                pass
            return
        
        # Tunnel established - now send the HTTP request through it
        # Rewrite request line: absolute URL -> relative path
        method = parts[0]
        http_ver = parts[2] if len(parts) > 2 else 'HTTP/1.1'
        new_request_line = f"{method} {path} {http_ver}\r\n".encode('utf-8')
        
        # Rebuild headers: remove Proxy-Authorization, ensure Host header exists
        rebuilt_headers = b""
        has_host = False
        for line in raw_headers.split(b"\r\n"):
            line_str = line.decode('utf-8', errors='ignore').strip()
            if not line_str:
                continue
            if line_str.lower().startswith('proxy-authorization:'):
                continue
            if line_str.lower().startswith('host:'):
                has_host = True
            rebuilt_headers += line + b"\r\n"
        
        if not has_host:
            host_header = f"Host: {dest_host}" + (f":{dest_port}" if dest_port != 80 else "")
            rebuilt_headers = host_header.encode('utf-8') + b"\r\n" + rebuilt_headers
        
        clash_writer.write(new_request_line + rebuilt_headers + b"\r\n")
        await clash_writer.drain()
        
        # Bidirectional pipe
        await asyncio.gather(
            self.pipe(client_reader, clash_writer, session_key, country, "tx"),
            self.pipe(clash_reader, client_writer, session_key, country, "rx"),
            return_exceptions=True
        )
        self.active_sessions.pop(session_key, None)
    
    # ==================== SOCKS5 HANDLER ====================
    
    async def _handle_socks5(self, client_reader, client_writer, first_byte):
        """Handle SOCKS5 proxy connection (original flow)."""
        try:
            # We already read the version byte (0x05), now read nmethods
            nmethods_byte = await client_reader.readexactly(1)
            nmethods = nmethods_byte[0]

            methods = await client_reader.readexactly(nmethods)
            
            auth_config = self.config.get("socks5_auth", {})
            auth_enabled = auth_config.get("enabled", False)
            auth_username = auth_config.get("username", "")
            auth_password = auth_config.get("password", "anypassword")

            username = None
            if auth_enabled:
                if 2 not in methods:
                    client_writer.write(bytes([5, 0xFF]))
                    await client_writer.drain()
                    client_writer.close()
                    return
                
                client_writer.write(bytes([5, 2]))
                await client_writer.drain()
                
                sub_ver = await client_reader.readexactly(1)
                if sub_ver[0] != 1:
                    client_writer.close()
                    return
                    
                u_len = (await client_reader.readexactly(1))[0]
                u_bytes = await client_reader.readexactly(u_len)
                username = u_bytes.decode('utf-8', errors='ignore')
                
                p_len = (await client_reader.readexactly(1))[0]
                p_bytes = await client_reader.readexactly(p_len)
                password = p_bytes.decode('utf-8', errors='ignore')
                
                requested_country = "GLOBAL"
                temp_user = username
                
                match = re.search(r'(?:^|-)(GLOBAL|Others|[A-Z]{2})-(rotate|sticky)(?:-|$)', temp_user, re.IGNORECASE)
                if match:
                    requested_country = match.group(1).upper()
                else:
                    parts = temp_user.split("-")
                    if len(parts) > 0 and len(parts[0]) == 2 and parts[0].isupper():
                        requested_country = parts[0]
                
                port_credentials = self.config.get("port_credentials", {})
                c_creds = port_credentials.get(requested_country, {})
                c_u = c_creds.get("username", "").strip()
                c_p = c_creds.get("password", "")
                
                expected_user_prefix = c_u if c_u else auth_username
                expected_password = c_p if c_p else auth_password
                
                if password != expected_password:
                    logger.warning(f"SOCKS5 Auth failed for user {username}: password mismatch.")
                    client_writer.write(bytes([1, 1]))
                    await client_writer.drain()
                    client_writer.close()
                    return
                
                if expected_user_prefix:
                    if username == expected_user_prefix:
                        username = ""
                    elif username.startswith(expected_user_prefix + "-"):
                        username = username[len(expected_user_prefix) + 1:]
                    else:
                        logger.warning(f"SOCKS5 Auth failed: username '{username}' does not match expected prefix '{expected_user_prefix}'.")
                        client_writer.write(bytes([1, 1]))
                        await client_writer.drain()
                        client_writer.close()
                        return

                client_writer.write(bytes([1, 0]))
                await client_writer.drain()
            else:
                if 0 in methods:
                    client_writer.write(bytes([5, 0]))
                    await client_writer.drain()
                elif 2 in methods:
                    client_writer.write(bytes([5, 2]))
                    await client_writer.drain()
                    
                    sub_ver = await client_reader.readexactly(1)
                    if sub_ver[0] != 1:
                        client_writer.close()
                        return
                        
                    u_len = (await client_reader.readexactly(1))[0]
                    u_bytes = await client_reader.readexactly(u_len)
                    username = u_bytes.decode('utf-8', errors='ignore')
                    
                    p_len = (await client_reader.readexactly(1))[0]
                    await client_reader.readexactly(p_len)
                    
                    client_writer.write(bytes([1, 0]))
                    await client_writer.drain()
                else:
                    client_writer.write(bytes([5, 0xFF]))
                    await client_writer.drain()
                    client_writer.close()
                    return

            # Parse route from username
            country, strategy, session_id = self.parse_username(username)
            
            session_key = f"{country}-{strategy}"
            if session_id:
                session_key += f"-{session_id}"
            self.active_sessions[session_key] = asyncio.get_event_loop().time()

            # Connection Request
            req_header = await client_reader.readexactly(4)
            cmd, atyp = req_header[1], req_header[3]
            
            if cmd != 1:
                client_writer.write(bytes([5, 7, 0, 1, 0, 0, 0, 0, 0, 0]))
                await client_writer.drain()
                client_writer.close()
                return

            if atyp == 1:
                addr_bytes = await client_reader.readexactly(4)
                dest_addr = socket.inet_ntoa(addr_bytes)
            elif atyp == 3:
                addr_len = (await client_reader.readexactly(1))[0]
                addr_bytes = await client_reader.readexactly(addr_len)
                dest_addr = addr_bytes.decode('utf-8', errors='ignore')
            elif atyp == 4:
                addr_bytes = await client_reader.readexactly(16)
                dest_addr = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            else:
                client_writer.write(bytes([5, 8, 0, 1, 0, 0, 0, 0, 0, 0]))
                await client_writer.drain()
                client_writer.close()
                return

            port_bytes = await client_reader.readexactly(2)
            dest_port = int.from_bytes(port_bytes, 'big')

            target_port = self.get_target_clash_port(country, strategy)
            
            logger.debug(f"SOCKS5 routing {username} to Mihomo port {target_port} -> {dest_addr}:{dest_port}")

            try:
                clash_reader, clash_writer = await asyncio.open_connection('127.0.0.1', target_port)
            except Exception as e:
                logger.error(f"Failed to connect to Mihomo port {target_port}: {e}")
                client_writer.write(bytes([5, 3, 0, 1, 0, 0, 0, 0, 0, 0]))
                await client_writer.drain()
                client_writer.close()
                return

            try:
                clash_writer.write(bytes([5, 1, 0]))
                await clash_writer.drain()
                
                clash_resp = await clash_reader.readexactly(2)
                if clash_resp[0] != 5 or clash_resp[1] != 0:
                    raise Exception("Mihomo rejected SOCKS5 handshake")

                clash_writer.write(bytes([5, 1, 0, atyp]) + addr_bytes + port_bytes)
                await clash_writer.drain()
                
                clash_conn_resp = await clash_reader.readexactly(4)
                if clash_conn_resp[1] != 0:
                    status = clash_conn_resp[1]
                    client_writer.write(bytes([5, status, 0, 1, 0, 0, 0, 0, 0, 0]))
                    await client_writer.drain()
                    clash_writer.close()
                    client_writer.close()
                    return

                c_atyp = clash_conn_resp[3]
                if c_atyp == 1:
                    await clash_reader.readexactly(6)
                elif c_atyp == 3:
                    c_len = (await clash_reader.readexactly(1))[0]
                    await clash_reader.readexactly(c_len + 2)
                elif c_atyp == 4:
                    await clash_reader.readexactly(18)

            except Exception as e:
                logger.error(f"Error handshaking with Mihomo: {e}")
                client_writer.write(bytes([5, 1, 0, 1, 0, 0, 0, 0, 0, 0]))
                await client_writer.drain()
                clash_writer.close()
                client_writer.close()
                return

            client_writer.write(bytes([5, 0, 0, 1, 0, 0, 0, 0, 0, 0]))
            await client_writer.drain()

            await asyncio.gather(
                self.pipe(client_reader, clash_writer, session_key, country, "tx"),
                self.pipe(clash_reader, client_writer, session_key, country, "rx"),
                return_exceptions=True
            )
            
            self.active_sessions.pop(session_key, None)

        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            logger.error(f"Error in SOCKS5 handler: {e}")


    def get_target_clash_port(self, country: str, strategy: str) -> int:
        """Find corresponding local port from core manager mappings."""
        # Check if country exists in ports
        ports = self.core_manager.country_ports
        
        # Fallback to GLOBAL if requested country is not present
        target_country = country
        if target_country not in ports:
            target_country = "GLOBAL"
            
        if target_country not in ports:
            # Absolute fallback
            return self.core_manager.port_pool_start
 
        return ports[target_country].get(strategy, ports[target_country]["rotate"])

    async def pipe(self, reader, writer, session_key=None, country="GLOBAL", direction="rx"):
        """Pipe data from reader to writer asynchronously."""
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                
                # Update session activity timestamp to prevent TTL timeout
                if session_key and session_key in self.active_sessions:
                    self.active_sessions[session_key] = asyncio.get_event_loop().time()
                
                # Track cumulative group traffic
                if country not in self.group_traffic:
                    self.group_traffic[country] = {"rx": 0, "tx": 0}
                self.group_traffic[country][direction] += len(data)
                    
                writer.write(data)
                await writer.drain()
        except:
            pass
        finally:
            try:
                writer.close()
            except:
                pass
