import asyncio
import json
import time
from collections import namedtuple
import aiohttp

LiveResult = namedtuple('LiveResult', [
    'status', 
    'message',
    'response',
    'card_info',
    'issuer',
    'country',
    'flag',
    'currency',
    'elapsed_time'
])

async def live_check(card: str) -> LiveResult:
    start_time = time.time()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    
    payload = {
        "data": card,
        "charge": False
    }
    
    result = LiveResult(
        status="Unknown",
        message="",
        response="",
        card_info="",
        issuer="",
        country="",
        flag="",
        currency="",
        elapsed_time=0
    )
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                "https://api.chkr.cc/",
                json=payload,
                timeout=30
            ) as response:
                response_text = await response.text()
                data = json.loads(response_text) if response_text else {}
                
                result = result._replace(
                    elapsed_time=time.time() - start_time,
                    response=response_text
                )
                
                if response.status == 200 and "code" in data:
                    if data["code"] == 1:
                        status = "Live ✅"
                    elif data["code"] == 0:
                        status = "Dead ❌"
                    else:
                        status = "Unknown"
                    
                    card_info = ""
                    issuer = ""
                    country = ""
                    flag = ""
                    currency = ""
                    
                    if "card" in data:
                        card_data = data["card"]
                        card_info = f"{card_data.get('category', '')} - {card_data.get('brand', '')} - {card_data.get('type', '')}".upper()
                        issuer = card_data.get('bank', 'Unknown')
                        
                        if "country" in card_data:
                            country = card_data["country"].get("name", "Unknown")
                            flag = card_data["country"].get("emoji", "")
                            currency = card_data["country"].get("currency", "")
                    
                    return result._replace(
                        status=status,
                        message=data.get("message", ""),
                        card_info=card_info,
                        issuer=issuer,
                        country=country,
                        flag=flag,
                        currency=currency
                    )
                else:
                    return result._replace(
                        status="Error",
                        message=f"API Error: {response.status}"
                    )
    except json.JSONDecodeError:
        return result._replace(
            status="Error",
            message="Invalid JSON response"
        )
    except asyncio.TimeoutError:
        return result._replace(
            status="Error",
            message="Request timed out"
        )
    except Exception as e:
        return result._replace(
            status="Error",
            message=str(e)
        )
