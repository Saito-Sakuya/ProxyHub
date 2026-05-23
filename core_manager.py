import os
import sys
import subprocess
import urllib.request
import zipfile
import yaml
import logging
import threading
import queue
import time
import shutil

logger = logging.getLogger("CoreManager")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

class CoreManager:
    def __init__(self, workspace_dir: str, config: dict):
        self.workspace_dir = workspace_dir
        self.config = config
        self.bin_dir = os.path.join(workspace_dir, "bin")
        
        import platform
        self.is_windows = platform.system().lower() == "windows"
        self.binary_name = "mihomo.exe" if self.is_windows else "mihomo"
        self.mihomo_path = os.path.join(self.bin_dir, self.binary_name)
        self.config_path = os.path.join(self.bin_dir, "config.yaml")
        
        # Ports mapping
        self.port_pool_start = config.get("port_pool_start", 20000)
        self.country_ports = {} # e.g. {"HK": {"rotate": 20002, "sticky": 20003}}
        
        # Process and logging
        self.process = None
        self.log_queue = queue.Queue(maxsize=1000)
        self.is_running = False
        self.log_thread = None

        # Ensure bin dir exists
        os.makedirs(self.bin_dir, exist_ok=True)

    def get_log_lines(self, count=100):
        """Fetch the latest log lines from the queue."""
        lines = []
        # We drain the queue up to count
        while not self.log_queue.empty() and len(lines) < count:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        return lines

    def add_log(self, line: str):
        """Helper to add log line with queue size limiting."""
        if self.log_queue.full():
            try:
                self.log_queue.get_nowait() # drop oldest
            except queue.Empty:
                pass
        self.log_queue.put(line)

    def check_and_download_core(self) -> bool:
        """Check if Mihomo core exists, and if not, download it from GitHub."""
        if os.path.exists(self.mihomo_path):
            logger.info(f"Mihomo core found at: {self.mihomo_path}")
            return True

        logger.info("Mihomo core not found. Starting automatic download...")
        self.add_log(f"[System] Mihomo core binary ({self.binary_name}) not found. Starting download...")

        # We download v1.18.0 which is stable and verified
        version = "v1.18.0"
        if self.is_windows:
            filename = f"mihomo-windows-amd64-{version}.zip"
        else:
            filename = f"mihomo-linux-amd64-{version}.gz"
            
        urls = [
            f"https://github.com/MetaCubeX/mihomo/releases/download/{version}/{filename}",
            f"https://mirror.ghproxy.com/https://github.com/MetaCubeX/mihomo/releases/download/{version}/{filename}" # Mirror for speed
        ]

        archive_path = os.path.join(self.bin_dir, "mihomo.archive")
        
        download_success = False
        for url in urls:
            try:
                logger.info(f"Trying to download from: {url}")
                self.add_log(f"[System] Downloading core from GitHub Release...")
                
                # Setup custom user agent
                req = urllib.request.Request(
                    url, 
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                )
                
                with urllib.request.urlopen(req, timeout=30) as response, open(archive_path, 'wb') as out_file:
                    shutil.copyfileobj(response, out_file)
                
                logger.info("Download completed successfully!")
                self.add_log("[System] Core download complete. Extracting files...")
                download_success = True
                break
            except Exception as e:
                logger.warning(f"Failed to download from {url}: {e}")
                self.add_log(f"[System] Download failed from current mirror: {e}")

        if not download_success:
            logger.error(f"All download mirrors failed. Please manually download Mihomo for your platform and place it in the 'bin' folder as '{self.binary_name}'.")
            self.add_log(f"[ERROR] Auto-download failed. Please download mihomo and place it in bin/ as '{self.binary_name}' manually.")
            return False

        # Extract Archive based on Platform
        try:
            if self.is_windows:
                # Extract Zip
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    # Find the executable in zip
                    for file_info in zip_ref.infolist():
                        if file_info.filename.endswith(".exe"):
                            # Extract this file
                            file_info.filename = "mihomo.exe" # rename to mihomo.exe
                            zip_ref.extract(file_info, self.bin_dir)
                            break
            else:
                # Extract Gzip
                import gzip
                with gzip.open(archive_path, 'rb') as f_in:
                    with open(self.mihomo_path, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                # Set execution permission on Linux
                os.chmod(self.mihomo_path, 0o755)
            
            # Clean up archive
            if os.path.exists(archive_path):
                os.remove(archive_path)
                
            logger.info("Mihomo core extracted and ready!")
            self.add_log("[System] Mihomo core extraction complete and ready!")
            return True
        except Exception as e:
            logger.error(f"Error unarchiving: {e}")
            self.add_log(f"[ERROR] Extraction failed: {e}")
            return False

    def generate_config(self, nodes: list) -> dict:
        """
        Dynamically generate Clash config.yaml based on parsed nodes.
        Returns a dict of country -> ports mappings for external routing.
        """
        # Filter only enabled nodes
        active_nodes = [node for node in nodes if node.get("_enabled", True) is not False]

        socks5_cfg = self.config.get("socks5_auth", {})
        socks5_auth_enabled = socks5_cfg.get("enabled", False)
        socks5_username = socks5_cfg.get("username", "").strip()
        socks5_password = socks5_cfg.get("password", "")
        if not socks5_username:
            socks5_username = "admin"

        if not active_nodes:
            logger.warning("No active/enabled nodes available to generate Clash config.")
            return {}

        # Reset country ports
        self.country_ports = {}
        
        # 1. Group active nodes by country
        country_groups = {} # e.g. {"HK": [node1, node2]}
        for node in active_nodes:
            country = node.get("_country", "Others")
            if country not in country_groups:
                country_groups[country] = []
            country_groups[country].append(node)

        # 2. Assign ports starting from port_pool_start
        current_port = self.port_pool_start
        
        # Assign Global ports first
        self.country_ports["GLOBAL"] = {
            "rotate": current_port,
            "sticky": current_port + 1
        }
        current_port += 2

        # Assign each country ports
        for country in sorted(country_groups.keys()):
            self.country_ports[country] = {
                "rotate": current_port,
                "sticky": current_port + 1
            }
            current_port += 2

        # 3. Build Clash YAML Configuration Structure
        # We strip out our internal '_country' property from proxies
        clash_proxies = []
        for node in active_nodes:
            clean_node = {k: v for k, v in node.items() if not k.startswith("_")}
            clash_proxies.append(clean_node)

        proxy_groups = []
        listeners = []
        rules = []

        # Add Global Proxy Groups
        node_names = [n["name"] for n in active_nodes]
        proxy_groups.append({
            "name": "Global-Rotate",
            "type": "load-balance",
            "strategy": "round-robin",
            "url": self.config.get("check_url", "http://www.gstatic.com/generate_204"),
            "interval": self.config.get("check_interval_seconds", 300),
            "proxies": node_names
        })
        proxy_groups.append({
            "name": "Global-Sticky",
            "type": "load-balance",
            "strategy": "consistent-hashing",
            "url": self.config.get("check_url", "http://www.gstatic.com/generate_204"),
            "interval": self.config.get("check_interval_seconds", 300),
            "proxies": node_names
        })

        # Add Global Listeners (bind to 127.0.0.1 - only SmartProxy connects internally, no auth needed)
        global_rotate_listener = {
            "name": "global-rotate",
            "type": "mixed",
            "port": self.country_ports["GLOBAL"]["rotate"],
            "listen": "127.0.0.1"
        }
        global_sticky_listener = {
            "name": "global-sticky",
            "type": "mixed",
            "port": self.country_ports["GLOBAL"]["sticky"],
            "listen": "127.0.0.1"
        }
        listeners.append(global_rotate_listener)
        listeners.append(global_sticky_listener)

        rules.append(f"IN-NAME,global-rotate,Global-Rotate")
        rules.append(f"IN-NAME,global-sticky,Global-Sticky")

        # Build country specific groups, listeners and rules
        for country, country_nodes in sorted(country_groups.items()):
            c_node_names = [n["name"] for n in country_nodes]
            
            rotate_group_name = f"{country}-Rotate"
            sticky_group_name = f"{country}-Sticky"

            # Proxy Groups
            proxy_groups.append({
                "name": rotate_group_name,
                "type": "load-balance",
                "strategy": "round-robin",
                "url": self.config.get("check_url", "http://www.gstatic.com/generate_204"),
                "interval": self.config.get("check_interval_seconds", 300),
                "proxies": c_node_names
            })
            proxy_groups.append({
                "name": sticky_group_name,
                "type": "load-balance",
                "strategy": "consistent-hashing",
                "url": self.config.get("check_url", "http://www.gstatic.com/generate_204"),
                "interval": self.config.get("check_interval_seconds", 300),
                "proxies": c_node_names
            })

            # Listeners (bind to 127.0.0.1, no auth - SmartProxy handles auth externally)
            c_rotate_listener = {
                "name": f"{country.lower()}-rotate",
                "type": "mixed",
                "port": self.country_ports[country]["rotate"],
                "listen": "127.0.0.1"
            }
            c_sticky_listener = {
                "name": f"{country.lower()}-sticky",
                "type": "mixed",
                "port": self.country_ports[country]["sticky"],
                "listen": "127.0.0.1"
            }
            listeners.append(c_rotate_listener)
            listeners.append(c_sticky_listener)

            # Rules
            rules.append(f"IN-NAME,{country.lower()}-rotate,{rotate_group_name}")
            rules.append(f"IN-NAME,{country.lower()}-sticky,{sticky_group_name}")

        # Final direct fallback rule
        rules.append("MATCH,DIRECT")

        # Full Clash YAML
        full_yaml = {
            "mode": "rule",
            "log-level": "info",
            "allow-lan": True,
            "bind-address": "*",
            "external-controller": "127.0.0.1:9090",
            "secret": "",
            "proxies": clash_proxies,
            "proxy-groups": proxy_groups,
            "listeners": listeners,
            "rules": rules
        }

        # Note: No global authentication needed since SmartProxy handles all auth
        # and internal listeners are bound to 127.0.0.1 only

        # Write config.yaml
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(full_yaml, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info(f"Generated config.yaml successfully with {len(nodes)} nodes across {len(country_groups)} countries.")
        self.add_log(f"[System] Generated Clash configuration file. Assigned ports for {len(country_groups)} countries.")
        return self.country_ports

    def _read_logs(self):
        """Thread worker to read stdout/stderr from Mihomo and put in queue."""
        while self.is_running and self.process:
            line = self.process.stdout.readline()
            if not line:
                break
            
            line_str = line.decode('utf-8', errors='ignore').strip()
            if line_str:
                self.add_log(f"[Mihomo] {line_str}")
                
        self.is_running = False
        logger.info("Mihomo log reading thread exited.")

    def start(self) -> bool:
        """Start the Mihomo core process."""
        if self.is_running:
            logger.warning("Mihomo core is already running.")
            return True

        if not os.path.exists(self.mihomo_path):
            logger.error("Mihomo core executable not found.")
            return False

        if not os.path.exists(self.config_path):
            logger.error("Clash config.yaml not found.")
            return False

        logger.info(f"Starting Mihomo core process...")
        self.add_log("[System] Starting Mihomo core backend process...")

        try:
            # Run in bin directory to handle config paths correctly
            self.process = subprocess.Popen(
                [self.mihomo_path, "-f", "config.yaml"],
                cwd=self.bin_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            
            self.is_running = True
            
            # Start logging background thread
            self.log_thread = threading.Thread(target=self._read_logs, daemon=True)
            self.log_thread.start()
            
            logger.info("Mihomo core process started successfully.")
            self.add_log("[System] Mihomo core started successfully!")
            return True
        except Exception as e:
            logger.error(f"Failed to start Mihomo: {e}")
            self.add_log(f"[ERROR] Failed to start Mihomo: {e}")
            self.is_running = False
            return False

    def stop(self):
        """Stop the Mihomo core process."""
        if not self.is_running or not self.process:
            return

        logger.info("Stopping Mihomo core process...")
        self.add_log("[System] Stopping Mihomo core backend...")
        
        self.is_running = False
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
            logger.info("Mihomo core process stopped.")
            self.add_log("[System] Mihomo core stopped.")
        except Exception as e:
            logger.warning(f"Error terminating Mihomo: {e}, killing process...")
            try:
                self.process.kill()
            except:
                pass
        self.process = None

    def restart(self, nodes: list) -> bool:
        """Regenerate config and restart the core process."""
        self.stop()
        time.sleep(1) # wait for resources release
        self.generate_config(nodes)
        return self.start()
