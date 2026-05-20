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
        """Handle incoming SOCKS5 client connection."""
        self.active_connections += 1
        peer = client_writer.get_extra_info('peername')
        logger.debug(f"New connection from {peer}")

        try:
            # 1. SOCKS5 Handshake / Method Selection
            header = await client_reader.readexactly(2)
            version, nmethods = header[0], header[1]
            
            if version != 5:
                logger.warning(f"Unsupported SOCKS version: {version}")
                client_writer.close()
                return

            methods = await client_reader.readexactly(nmethods)
            
            auth_config = self.config.get("socks5_auth", {})
            auth_enabled = auth_config.get("enabled", False)
            auth_username = auth_config.get("username", "")
            auth_password = auth_config.get("password", "anypassword")

            username = None
            if auth_enabled:
                if 2 not in methods:
                    # Require Auth, but client doesn't support it
                    client_writer.write(bytes([5, 0xFF]))
                    await client_writer.drain()
                    client_writer.close()
                    return
                
                # Accept Username/Password Auth
                client_writer.write(bytes([5, 2]))
                await client_writer.drain()
                
                # Auth Subnegotiation
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
                
                # To support per-country credentials, we determine the country code from the requested username
                requested_country = "GLOBAL"
                temp_user = username
                
                # Check for standard suffix patterns: -rotate or -sticky
                match = re.search(r'(?:^|-)(GLOBAL|Others|[A-Z]{2})-(rotate|sticky)(?:-|$)', temp_user, re.IGNORECASE)
                if match:
                    requested_country = match.group(1).upper()
                else:
                    parts = temp_user.split("-")
                    if len(parts) > 0 and len(parts[0]) == 2 and parts[0].isupper():
                        requested_country = parts[0]
                
                # Retrieve country-specific credentials with fallback to global default
                port_credentials = self.config.get("port_credentials", {})
                c_creds = port_credentials.get(requested_country, {})
                c_u = c_creds.get("username", "").strip()
                c_p = c_creds.get("password", "")
                
                expected_user_prefix = c_u if c_u else auth_username
                expected_password = c_p if c_p else auth_password
                
                if password != expected_password:
                    logger.warning(f"SOCKS5 Auth failed for user {username}: password mismatch.")
                    # Auth failure status (0x01 status != 0x00, e.g. 0x01)
                    client_writer.write(bytes([1, 1]))
                    await client_writer.drain()
                    client_writer.close()
                    return
                
                # Verify custom username if specified
                if expected_user_prefix:
                    if username == expected_user_prefix:
                        # Exact match, default routing (GLOBAL-rotate)
                        username = ""
                    elif username.startswith(expected_user_prefix + "-"):
                        # Custom username prefix matches, strip prefix for routing
                        username = username[len(expected_user_prefix) + 1:]
                    else:
                        logger.warning(f"SOCKS5 Auth failed: username '{username}' does not match expected prefix '{expected_user_prefix}'.")
                        client_writer.write(bytes([1, 1]))
                        await client_writer.drain()
                        client_writer.close()
                        return

                # Auth success (0x01 status 0x00)
                client_writer.write(bytes([1, 0]))
                await client_writer.drain()
            else:
                # Auth is NOT enabled. Support both No Auth (0x00) and Username/Password (0x02)
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
            
            # Session Tracking for Dashboard
            session_key = f"{country}-{strategy}"
            if session_id:
                session_key += f"-{session_id}"
            
            # Register active session with last active timestamp
            self.active_sessions[session_key] = asyncio.get_event_loop().time()

            # 2. Connection Request
            req_header = await client_reader.readexactly(4)
            cmd, atyp = req_header[1], req_header[3]
            
            if cmd != 1: # Only CONNECT supported
                client_writer.write(bytes([5, 7, 0, 1, 0, 0, 0, 0, 0, 0])) # Command not supported
                await client_writer.drain()
                client_writer.close()
                return

            # Read target address and port
            if atyp == 1: # IPv4
                addr_bytes = await client_reader.readexactly(4)
                dest_addr = socket.inet_ntoa(addr_bytes)
            elif atyp == 3: # Domain Name
                addr_len = (await client_reader.readexactly(1))[0]
                addr_bytes = await client_reader.readexactly(addr_len)
                dest_addr = addr_bytes.decode('utf-8', errors='ignore')
            elif atyp == 4: # IPv6
                addr_bytes = await client_reader.readexactly(16)
                dest_addr = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            else:
                client_writer.write(bytes([5, 8, 0, 1, 0, 0, 0, 0, 0, 0])) # Address type not supported
                await client_writer.drain()
                client_writer.close()
                return

            port_bytes = await client_reader.readexactly(2)
            dest_port = int.from_bytes(port_bytes, 'big')

            # 3. Connect to local Clash listener port for this country/strategy
            target_port = self.get_target_clash_port(country, strategy)
            
            logger.debug(f"Routing session {username} to Clash port {target_port} -> {dest_addr}:{dest_port}")

            try:
                clash_reader, clash_writer = await asyncio.open_connection('127.0.0.1', target_port)
            except Exception as e:
                logger.error(f"Failed to connect to local Clash port {target_port}: {e}")
                client_writer.write(bytes([5, 3, 0, 1, 0, 0, 0, 0, 0, 0])) # Network unreachable
                await client_writer.drain()
                client_writer.close()
                return

            # 4. Perform SOCKS5 handshake with Clash mixed port
            try:
                clash_writer.write(bytes([5, 1, 0])) # SOCKS5, 1 method: No Auth
                await clash_writer.drain()
                
                clash_resp = await clash_reader.readexactly(2)
                if clash_resp[0] != 5 or clash_resp[1] != 0:
                    raise Exception("Clash rejected SOCKS5 handshake")

                # Send connection request to Clash
                clash_writer.write(bytes([5, 1, 0, atyp]) + addr_bytes + port_bytes)
                await clash_writer.drain()
                
                clash_conn_resp = await clash_reader.readexactly(4)
                if clash_conn_resp[1] != 0: # Connection failed
                    status = clash_conn_resp[1]
                    client_writer.write(bytes([5, status, 0, 1, 0, 0, 0, 0, 0, 0]))
                    await client_writer.drain()
                    clash_writer.close()
                    client_writer.close()
                    return

                # Read remaining address info from Clash response to clear the buffer
                c_atyp = clash_conn_resp[3]
                if c_atyp == 1:
                    await clash_reader.readexactly(6) # 4 bytes IP + 2 bytes port
                elif c_atyp == 3:
                    c_len = (await clash_reader.readexactly(1))[0]
                    await clash_reader.readexactly(c_len + 2)
                elif c_atyp == 4:
                    await clash_reader.readexactly(18) # 16 bytes IPv6 + 2 bytes port

            except Exception as e:
                logger.error(f"Error handshaking with Clash: {e}")
                client_writer.write(bytes([5, 1, 0, 1, 0, 0, 0, 0, 0, 0])) # General failure
                await client_writer.drain()
                clash_writer.close()
                client_writer.close()
                return

            # 5. Success! Reply to original client
            client_writer.write(bytes([5, 0, 0, 1, 0, 0, 0, 0, 0, 0])) # Success
            await client_writer.drain()

            # 6. Pipe data bi-directionally
            await asyncio.gather(
                self.pipe(client_reader, clash_writer, session_key, country, "tx"),
                self.pipe(clash_reader, client_writer, session_key, country, "rx"),
                return_exceptions=True
            )
            
            self.active_sessions.pop(session_key, None)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in SOCKS5 handler: {e}")
        finally:
            self.active_connections -= 1
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except:
                pass

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
