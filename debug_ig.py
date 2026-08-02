import os
import requests
from dotenv import load_dotenv

load_dotenv()

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
IG_HOST = "instagram-statistics-api.p.rapidapi.com"

headers = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": IG_HOST,
}
params = {
    "q": "pet",
    "page": "1",
    "perPage": "10",
}

resp = requests.get(f"https://{IG_HOST}/search", headers=headers, params=params, timeout=12)
print("状态码:", resp.status_code)
print("原始响应:")
print(resp.text[:3000])