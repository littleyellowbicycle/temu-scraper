#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Temu Product Scraper
====================
爬取 Temu 商品详情页数据，按照指定的 JSON 结构组织输出。

依赖安装:
    pip install requests beautifulsoup4 lxml
    可选增强:
    pip install cloudscraper        # 绕过 Cloudflare 防护
    pip install curl_cffi           # 浏览器 TLS 指纹伪装

使用方式:
    python temu_scraper.py "https://www.temu.com/xxxxx-g-123456.html"
    python temu_scraper.py "https://www.temu.com/xxxxx-g-123456.html" ./output

环境变量:
    TEMU_COOKIE       - 设置 Cookie（提升访问成功率）
    TEMU_FORCE_ENGINE - 强制使用指定抓取引擎: requests / cloudscraper / curl_cffi
    HTTP_PROXY        - 设置 HTTP 代理
    HTTPS_PROXY       - 设置 HTTPS 代理
"""

import sys
import os
import re
import json
import time
import itertools
import logging
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup, Tag

# 可选依赖：Cloudflare 绕过
try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

# 可选依赖：TLS 指纹伪装
try:
    from curl_cffi import requests as curl_cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


# ========================= 配置 =========================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("TemuScraper")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Referer": "https://www.temu.com/",
    "Origin": "https://www.temu.com",
    # Chrome 浏览器特征头
    "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Dnt": "1",
}

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 5


# ========================= 工具函数 =========================

def extract_goods_id(url):
    """
    从 Temu URL 中提取商品ID (goods_id)

    Temu URL 格式举例:
      - https://www.temu.com/xxxxx-g-123456.html          -> 123456
      - https://www.temu.com/xxxxx-d-123456.html          -> 123456
      - https://www.temu.com/product-detail-g-123456.html  -> 123456
      - https://www.temu.com/goods.html?goods_id=123456    -> 123456
    """
    # 模式1: -g-数字
    m = re.search(r"[-/]g[-/](\d+)", url)
    if m:
        return m.group(1)
    # 模式2: -d-数字
    m = re.search(r"[-/]d[-/](\d+)", url)
    if m:
        return m.group(1)
    # 模式3: URL参数
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ["goods_id", "goodsId", "id"]:
        if key in qs:
            return qs[key][0]
    # 模式4: 路径中纯数字
    m = re.search(r"/(\d+)\.html", url)
    if m:
        return m.group(1)
    raise ValueError(f"无法从 URL 中提取商品ID: {url}")


def safe_text(element):
    if element is None:
        return ""
    if isinstance(element, Tag):
        return element.get_text(strip=True)
    return str(element).strip()


def safe_float(text, default=0.0):
    text = re.sub(r"[^\d.]", "", str(text))
    try:
        return float(text) if text else default
    except ValueError:
        return default


def clean_price(text):
    m = re.search(r"[\d,]+\.?\d*", str(text).replace(",", ""))
    return m.group(0) if m else "0.00"


def extract_currency_symbol(text):
    m = re.search(r"[¥€£$₹]", str(text))
    return m.group(0) if m else "$"


def build_proxy_dict():
    http_proxy = os.environ.get("HTTP_PROXY", os.environ.get("http_proxy", ""))
    https_proxy = os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy", ""))
    proxies = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    elif http_proxy:
        proxies["https"] = http_proxy
    return proxies if proxies else None


def deep_get(data, *keys, default=None):
    """安全地从嵌套字典中获取值"""
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        elif isinstance(current, list) and isinstance(key, int) and key < len(current):
            current = current[key]
        else:
            return default
        if current is None:
            return default
    return current


# ========================= 页面抓取 =========================

def _get_engine_order():
    """按优先级返回可用的抓取引擎列表"""
    forced = os.environ.get("TEMU_FORCE_ENGINE", "").lower()
    if forced:
        if forced == "cloudscraper" and HAS_CLOUDSCRAPER:
            return ["cloudscraper"]
        elif forced == "curl_cffi" and HAS_CURL_CFFI:
            return ["curl_cffi"]
        elif forced == "requests":
            return ["requests"]
        else:
            logger.warning(f"强制引擎 {forced} 不可用，回退到自动选择")

    engines = []
    if HAS_CLOUDSCRAPER:
        engines.append("cloudscraper")
    if HAS_CURL_CFFI:
        engines.append("curl_cffi")
    engines.append("requests")
    return engines


def _fetch_with_cloudscraper(url, session=None):
    """使用 cloudscraper 抓取（绕过 Cloudflare）"""
    headers = DEFAULT_HEADERS.copy()
    cookie = os.environ.get("TEMU_COOKIE", "")
    if cookie:
        headers["Cookie"] = cookie
    proxies = build_proxy_dict()

    scraper = session if session else cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[cloudscraper] 请求页面 (尝试 {attempt}/{MAX_RETRIES}): {url}")
            resp = scraper.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                               proxies=proxies)
            if resp.status_code == 200:
                if "captcha" in resp.text.lower() or "verification" in resp.text.lower():
                    logger.warning("检测到验证码页面")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY * 2)
                        continue
                logger.info(f"页面抓取成功，内容长度: {len(resp.text)}")
                return resp.text
            elif resp.status_code in (403, 429):
                logger.warning(f"{resp.status_code} - 被限流/拒绝")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            elif resp.status_code == 404:
                raise ValueError(f"商品页面不存在: {url}")
            else:
                logger.warning(f"HTTP {resp.status_code}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
        except Exception as e:
            logger.error(f"cloudscraper 请求异常: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
    raise RuntimeError(f"[cloudscraper] 抓取失败，已重试 {MAX_RETRIES} 次: {url}")


def _fetch_with_curl_cffi(url, session=None):
    """使用 curl_cffi 抓取（TLS 指纹伪装为 Chrome 120）"""
    headers = DEFAULT_HEADERS.copy()
    cookie = os.environ.get("TEMU_COOKIE", "")
    if cookie:
        headers["Cookie"] = cookie
    proxies = build_proxy_dict()

    sess = session if session else curl_cffi_requests.Session()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[curl_cffi] 请求页面 (尝试 {attempt}/{MAX_RETRIES}): {url}")
            resp = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                            proxies=proxies, impersonate="chrome120",
                            allow_redirects=True)
            if resp.status_code == 200:
                if "captcha" in resp.text.lower() or "verification" in resp.text.lower():
                    logger.warning("检测到验证码页面")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY * 2)
                        continue
                logger.info(f"页面抓取成功，内容长度: {len(resp.text)}")
                return resp.text
            elif resp.status_code in (403, 429):
                logger.warning(f"{resp.status_code} - 被限流/拒绝")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            elif resp.status_code == 404:
                raise ValueError(f"商品页面不存在: {url}")
            else:
                logger.warning(f"HTTP {resp.status_code}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
        except Exception as e:
            logger.error(f"curl_cffi 请求异常: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
    raise RuntimeError(f"[curl_cffi] 抓取失败，已重试 {MAX_RETRIES} 次: {url}")


def _fetch_with_requests(url, session=None):
    """使用标准 requests 库抓取"""
    headers = DEFAULT_HEADERS.copy()
    cookie = os.environ.get("TEMU_COOKIE", "")
    if cookie:
        headers["Cookie"] = cookie
    proxies = build_proxy_dict()
    sess = session or requests.Session()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[requests] 请求页面 (尝试 {attempt}/{MAX_RETRIES}): {url}")
            resp = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                            allow_redirects=True, proxies=proxies)
            if resp.status_code == 200:
                if "captcha" in resp.text.lower() or "verification" in resp.text.lower():
                    logger.warning("检测到验证码页面")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY * 2)
                        continue
                logger.info(f"页面抓取成功，内容长度: {len(resp.text)}")
                return resp.text
            elif resp.status_code in (403, 429):
                logger.warning(f"{resp.status_code} - 被限流/拒绝")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            elif resp.status_code == 404:
                raise ValueError(f"商品页面不存在: {url}")
            else:
                logger.warning(f"HTTP {resp.status_code}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
        except requests.RequestException as e:
            logger.error(f"requests 请求异常: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
    raise RuntimeError(f"[requests] 抓取失败，已重试 {MAX_RETRIES} 次: {url}")


def fetch_page(url, session=None):
    """
    按优先级尝试可用引擎抓取页面。
    优先级: cloudscraper > curl_cffi > requests
    可通过环境变量 TEMU_FORCE_ENGINE 强制指定。
    """
    engines = _get_engine_order()
    logger.info(f"可用抓取引擎: {engines}")

    last_error = None
    for engine in engines:
        try:
            if engine == "cloudscraper":
                return _fetch_with_cloudscraper(url)
            elif engine == "curl_cffi":
                return _fetch_with_curl_cffi(url)
            else:
                return _fetch_with_requests(url, session)
        except RuntimeError as e:
            last_error = e
            logger.warning(f"引擎 {engine} 失败，尝试下一个...")

    raise last_error or RuntimeError(f"所有抓取引擎均失败: {url}")


# ========================= 页面解析 =========================

class TemuProductParser:
    """Temu 商品页面解析器"""

    def __init__(self, html, source_url):
        self.soup = BeautifulSoup(html, "lxml")
        self.source_url = source_url
        self.goods_id = extract_goods_id(source_url)
        self._page_data = None

    # ---------- 页面内嵌数据提取 ----------

    def _extract_page_data(self):
        """
        从页面内嵌的 <script> 标签中提取商品数据

        Temu 页面通常在以下位置嵌入数据：
          1. window.rawData = {...}  (新版 React SSR)
          2. window.__INITIAL_STATE__ = {...}
          3. window.__NEXT_DATA__ = {...}
          4. <script type="application/ld+json">
        """
        if self._page_data is not None:
            return self._page_data

        page_data = {}

        # 新版 Temu: window.rawData (brace-match 提取)
        rawdata_m = re.search(r'window\.rawData\s*=\s*(\{)', self.soup.prettify() if not hasattr(self, '_raw_html') else getattr(self, '_raw_html', ''))
        # 直接在原始 HTML 中搜索
        html_text = str(self.soup)
        idx = html_text.find('window.rawData')
        if idx >= 0:
            start = html_text.find('{', idx)
            if start >= 0:
                depth = 0
                for i in range(start, len(html_text)):
                    if html_text[i] == '{':
                        depth += 1
                    elif html_text[i] == '}':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            try:
                                raw = html_text[start:end]
                                raw = re.sub(r'\bundefined\b', 'null', raw)
                                page_data["raw_data"] = json.loads(raw)
                                logger.info("成功提取 window.rawData 数据")
                            except json.JSONDecodeError as e:
                                logger.warning(f"解析 window.rawData 失败: {e}")
                            break

        for script in self.soup.select("script"):
            text = script.string or ""
            if not text:
                continue

            # __INITIAL_STATE__
            m = re.search(
                r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*$",
                text, re.DOTALL | re.MULTILINE,
            )
            if m:
                try:
                    raw = m.group(1)
                    raw = re.sub(r'\bundefined\b', 'null', raw)
                    page_data["initial_state"] = json.loads(raw)
                    logger.info("成功提取 __INITIAL_STATE__ 数据")
                except json.JSONDecodeError as e:
                    logger.warning(f"解析 __INITIAL_STATE__ 失败: {e}")

            # __NEXT_DATA__
            m = re.search(
                r"window\.__NEXT_DATA__\s*=\s*(\{.*?\})\s*;?\s*$",
                text, re.DOTALL | re.MULTILINE,
            )
            if m:
                try:
                    raw = m.group(1)
                    raw = re.sub(r'\bundefined\b', 'null', raw)
                    page_data["next_data"] = json.loads(raw)
                    logger.info("成功提取 __NEXT_DATA__ 数据")
                except json.JSONDecodeError as e:
                    logger.warning(f"解析 __NEXT_DATA__ 失败: {e}")

        # <script type="application/ld+json">
        for script in self.soup.select('script[type="application/ld+json"]'):
            text = script.string or ""
            if not text:
                continue
            try:
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        page_data["ld_json"] = item
                        break
            except json.JSONDecodeError:
                continue

        self._page_data = page_data
        return page_data

    def _get_product_data(self):
        """从内嵌数据中获取商品核心数据"""
        pd = self._extract_page_data()
        candidates = [
            # 新版 Temu: window.rawData.store
            lambda: deep_get(pd, "raw_data", "store", "goods"),
            lambda: deep_get(pd, "raw_data", "store"),
            lambda: deep_get(pd, "raw_data", "goods"),
            lambda: deep_get(pd, "raw_data"),
            # 旧版路径
            lambda: deep_get(pd, "initial_state", "goodsDetail", "goodsDetail"),
            lambda: deep_get(pd, "initial_state", "goodsDetail"),
            lambda: deep_get(pd, "initial_state", "goods", "detail"),
            lambda: deep_get(pd, "initial_state", "product", "detail"),
            lambda: deep_get(pd, "initial_state", "detail"),
            lambda: deep_get(pd, "next_data", "props", "pageProps", "goodsDetail"),
            lambda: deep_get(pd, "next_data", "props", "pageProps", "goods", "detail"),
            lambda: deep_get(pd, "next_data", "props", "pageProps", "product"),
            lambda: deep_get(pd, "next_data", "props", "initialState", "goodsDetail"),
            lambda: deep_get(pd, "ld_json"),
        ]
        for fn in candidates:
            result = fn()
            if result and isinstance(result, dict) and len(result) > 0:
                logger.info(f"从内嵌数据路径获取到商品数据，字段数: {len(result)}")
                return result
        return {}

    # ---------- 主商品信息 ----------

    def parse_title(self):
        """解析商品标题"""
        pdata = self._get_product_data()
        for key in ["title", "goodsName", "goods_name", "name"]:
            val = pdata.get(key, "")
            if val:
                return str(val)
        meta = self.soup.select_one('meta[property="og:title"]')
        if meta:
            return meta.get("content", "")
        title_el = self.soup.select_one("h1, [class*='title']")
        if title_el:
            return safe_text(title_el)
        title_tag = self.soup.select_one("title")
        if title_tag:
            return safe_text(title_tag).replace("- Temu", "").strip()
        return ""

    def parse_price(self):
        """
        解析价格，返回 (price_str, currency_symbol)
        Temu 价格通常以分为单位存储
        """
        pdata = self._get_product_data()
        price_candidates = [
            "salePrice", "price", "minPrice", "sale_price",
            "discountPrice", "appPrice",
        ]
        for field in price_candidates:
            val = pdata.get(field)
            if val is not None:
                if isinstance(val, dict):
                    # {"amount": 2089, "currencyCode": "USD"}
                    amount = val.get("amount") or val.get("value") or val.get("cent")
                    currency_code = val.get("currencyCode") or val.get("currency", "USD")
                    if amount is not None:
                        try:
                            price_float = float(amount) / 100
                        except (ValueError, TypeError):
                            price_float = safe_float(str(amount))
                        currency_map = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩"}
                        return f"{price_float:.2f}", currency_map.get(currency_code, "$")
                elif isinstance(val, (int, float)):
                    try:
                        price_float = float(val) / 100
                    except (ValueError, TypeError):
                        price_float = safe_float(str(val))
                    return f"{price_float:.2f}", "$"
                elif isinstance(val, str):
                    currency = extract_currency_symbol(val)
                    price_val = clean_price(val)
                    if price_val and price_val != "0.00":
                        return price_val, currency

        # HTML 选择器
        price_selectors = [
            '[class*="price"] [class*="sale"]',
            '[class*="price"] [class*="discount"]',
            '[class*="price"] [class*="current"]',
            '[data-testid="product-price"]',
            '[class*="Price"] [class*="Sale"]',
            '.price_current',
            '[class*="priceWrap"] span',
        ]
        for sel in price_selectors:
            el = self.soup.select_one(sel)
            if el:
                price_text = safe_text(el)
                currency = extract_currency_symbol(price_text)
                price_val = clean_price(price_text)
                if price_val and price_val != "0.00":
                    return price_val, currency

        # meta 标签
        meta = self.soup.select_one('meta[property="product:price:amount"]')
        if meta:
            price_val = meta.get("content", "")
            meta_currency = self.soup.select_one('meta[property="product:price:currency"]')
            currency_code = meta_currency.get("content", "USD") if meta_currency else "USD"
            currency_map = {"USD": "$", "EUR": "€", "GBP": "£"}
            return clean_price(price_val), currency_map.get(currency_code, "$")

        # JSON-LD
        ld = self._extract_page_data().get("ld_json", {})
        if ld:
            offers = ld.get("offers", {})
            price = offers.get("price", "")
            if price:
                return clean_price(str(price)), "$"

        return "0.00", "$"

    def parse_brand(self):
        """解析品牌/店铺信息"""
        pdata = self._get_product_data()
        for key in ["brand", "brandName", "brand_name", "mallName", "shopName", "shop_name"]:
            val = pdata.get(key, "")
            if val:
                return str(val)
        brand_el = self.soup.select_one(
            '[class*="brand"], [class*="store"], [class*="shop"], [class*="mall"]')
        if brand_el:
            text = safe_text(brand_el)
            if text:
                return text
        ld = self._extract_page_data().get("ld_json", {})
        brand = ld.get("brand", {})
        if isinstance(brand, dict):
            return brand.get("name", "")
        return ""

    def parse_categories(self):
        """解析分类路径"""
        categories = []
        pdata = self._get_product_data()
        cat_data = pdata.get("category") or pdata.get("categories") or pdata.get("catPath", [])
        if isinstance(cat_data, list):
            for item in cat_data:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("catName") or item.get("categoryName", "")
                    if name and name not in categories:
                        categories.append(str(name))
                elif isinstance(item, str) and item not in categories:
                    categories.append(item)
        elif isinstance(cat_data, str) and cat_data:
            categories = [c.strip() for c in cat_data.split("/") if c.strip()]
        if categories:
            return categories
        # 面包屑
        for a in self.soup.select(
            '[class*="breadcrumb"] a, [class*="Breadcrumb"] a, '
            '[class*="crumb"] a, nav a'
        ):
            text = safe_text(a)
            if text and text not in categories and text.lower() not in ("home", "temu"):
                categories.append(text)
        return categories

    def parse_images(self):
        """解析主图列表"""
        images, seen = [], set()
        pdata = self._get_product_data()
        img_data = pdata.get("gallery") or pdata.get("images") or pdata.get("galleryList") or pdata.get("imgs", [])
        if isinstance(img_data, list):
            for item in img_data:
                if isinstance(item, dict):
                    url = item.get("url") or item.get("src") or item.get("imageUrl") or item.get("thumbUrl", "")
                    if url and url not in seen:
                        images.append(str(url))
                        seen.add(url)
                elif isinstance(item, str) and item not in seen:
                    images.append(item)
                    seen.add(item)
        if images:
            return images
        # HTML 图片元素
        for img in self.soup.select(
            '[class*="gallery"] img, [class*="swiper"] img, '
            '[class*="carousel"] img, [class*="slider"] img, '
            '[class*="mainImage"] img, [class*="productImg"] img, '
            'picture img'
        ):
            src = img.get("data-src") or img.get("src", "")
            if src and not src.startswith("data:") and "icon" not in src and "logo" not in src:
                if src not in seen:
                    images.append(src)
                    seen.add(src)
        return images

    def parse_about_this_item(self):
        """解析商品要点"""
        bullets = []
        pdata = self._get_product_data()
        desc_list = pdata.get("description") or pdata.get("highlights") or pdata.get("features") or []
        if isinstance(desc_list, list):
            for item in desc_list:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or item.get("desc", "")
                    if text:
                        bullets.append(str(text))
                elif isinstance(item, str) and item:
                    bullets.append(item)
        elif isinstance(desc_list, str) and desc_list:
            bullets.append(desc_list)
        if bullets:
            return bullets
        # HTML
        for li in self.soup.select(
            '[class*="description"] li, [class*="feature"] li, '
            '[class*="highlight"] li, [class*="detail"] li, '
            '[class*="specification"] li, [class*="bullet"] li'
        ):
            text = safe_text(li)
            if text and len(text) > 5:
                bullets.append(text)
        return bullets

    def parse_product_description(self):
        """解析商品描述"""
        pdata = self._get_product_data()
        for key in ["productDescription", "product_description", "desc", "description", "content"]:
            val = pdata.get(key, "")
            if val and isinstance(val, str):
                return val
        desc_el = self.soup.select_one(
            '[class*="productDesc"], [class*="product-desc"], '
            '[class*="goodsDesc"], [class*="goods-desc"], '
            '[class*="detailContent"]')
        if desc_el:
            return safe_text(desc_el)
        return ""

    def parse_product_details(self):
        """解析商品详细信息表"""
        details = {}
        pdata = self._get_product_data()
        # 字段映射
        detail_field_mapping = {
            "brand": "Brand Name", "brandName": "Brand Name",
            "material": "Material Type", "materialType": "Material Type",
            "color": "Color", "size": "Size",
            "weight": "Item Weight", "itemWeight": "Item Weight",
            "capacity": "Capacity", "origin": "Country of Origin",
            "countryOfOrigin": "Country of Origin",
            "manufacturer": "Manufacturer",
            "model": "Model Number", "modelNumber": "Model Number",
            "pattern": "Pattern", "shape": "Shape",
            "theme": "Theme", "style": "Product Style",
            "productStyle": "Product Style",
            "careInstructions": "Product Care Instructions",
            "feature": "Material Features",
            "materialFeature": "Material Features",
            "reusable": "Reusability",
            "bpaFree": "Material Type Free",
            "finish": "Finish Types",
            "specialFeature": "Other Special Features of the Product",
            "upc": "UPC", "ean": "EAN",
            "ageRange": "Age Range Description",
            "numberOfItems": "Number of Items",
            "unitCount": "Unit Count",
        }
        for src_key, dst_key in detail_field_mapping.items():
            val = pdata.get(src_key)
            if val is not None and str(val).strip():
                details[dst_key] = str(val)
        # 规格列表
        specs_list = (
            pdata.get("specs") or pdata.get("attributes") or
            pdata.get("specifications") or pdata.get("attrList") or []
        )
        if isinstance(specs_list, list):
            for item in specs_list:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("key") or item.get("attrName") or item.get("label", "")
                    val = item.get("value") or item.get("attrValue") or item.get("val") or item.get("content", "")
                    if key and val:
                        details[str(key)] = str(val)
        if details:
            return details
        # HTML 表格/列表
        for row in self.soup.select(
            '[class*="specification"] tr, [class*="spec"] tr, '
            '[class*="attribute"] tr, [class*="detail"] tr'
        ):
            cells = row.select("td, th")
            if len(cells) >= 2:
                key = safe_text(cells[0]).rstrip(":")
                val = safe_text(cells[1])
                if key and val:
                    details[key] = val
        for item in self.soup.select(
            '[class*="specification"] li, [class*="attribute"] li, '
            '[class*="spec"] li, [class*="property"] li'
        ):
            text = safe_text(item)
            if ":" in text:
                parts = text.split(":", 1)
                key, val = parts[0].strip(), parts[1].strip()
                if key and val:
                    details[key] = val
        return details

    # ---------- SKU / 变体解析 ----------

    def parse_skus(self):
        """解析所有 SKU 变体"""
        skus = []
        pdata = self._get_product_data()
        sku_list = (
            pdata.get("skus") or pdata.get("skuList") or
            pdata.get("variants") or pdata.get("variantList") or
            pdata.get("specList") or []
        )
        if isinstance(sku_list, list) and len(sku_list) > 0:
            price_val, currency = self.parse_price()
            for idx, sku_item in enumerate(sku_list):
                if not isinstance(sku_item, dict):
                    continue
                sku = self._build_sku_from_data(sku_item, idx, price_val, currency)
                if sku:
                    skus.append(sku)
        if not skus:
            skus = self._build_skus_from_specs()
        if not skus:
            price_val, currency = self.parse_price()
            spec_kv, spec_v = self._parse_current_specs()
            gallery = self._parse_current_sku_gallery()
            skus.append({
                "skuId": self.goods_id,
                "goodsId": self.goods_id,
                "goodsName": self.parse_title(),
                "thumbUrl": gallery[0]["url"] if gallery else "",
                "currency": currency,
                "price": safe_float(price_val),
                "specKeyValues": spec_kv,
                "specValues": spec_v,
                "skuGallery": gallery,
                "isskuGallery": 1,
                "url": self.source_url,
            })
        return skus

    def _build_sku_from_data(self, sku_item, index, default_price, default_currency):
        """从内嵌数据中的 SKU 对象构建标准格式"""
        sku_id = str(
            sku_item.get("skuId") or sku_item.get("id") or
            sku_item.get("sku_id") or sku_item.get("skuCode") or
            f"{self.goods_id}_{index}"
        )
        goods_name = str(
            sku_item.get("skuName") or sku_item.get("name") or
            sku_item.get("title") or self.parse_title()
        )

        # 价格处理（Temu价格以分为单位）
        sku_price = None
        currency = default_currency
        price_field = sku_item.get("price") or sku_item.get("salePrice") or sku_item.get("sale_price")
        if isinstance(price_field, dict):
            amount = price_field.get("amount") or price_field.get("value") or price_field.get("cent")
            cc = price_field.get("currencyCode") or price_field.get("currency", "USD")
            if amount is not None:
                try:
                    sku_price = float(amount) / 100
                except (ValueError, TypeError):
                    sku_price = safe_float(str(amount))
                currency_map = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
                currency = currency_map.get(cc, "$")
        elif price_field is not None:
            if isinstance(price_field, (int, float)):
                try:
                    sku_price = float(price_field) / 100
                except (ValueError, TypeError):
                    sku_price = safe_float(str(price_field))
            elif isinstance(price_field, str):
                sku_price = safe_float(clean_price(price_field))
        if sku_price is None:
            sku_price = safe_float(default_price)

        # 规格
        spec_parts, spec_val_parts = [], []
        spec_groups = (
            sku_item.get("specList") or sku_item.get("specs") or
            sku_item.get("attributes") or sku_item.get("attrs") or
            sku_item.get("specValues") or sku_item.get("skcSpecList") or []
        )
        if isinstance(spec_groups, list):
            for spec in spec_groups:
                if isinstance(spec, dict):
                    spec_name = spec.get("name") or spec.get("key") or spec.get("specName") or spec.get("attrName", "")
                    spec_value = spec.get("value") or spec.get("val") or spec.get("attrValue") or spec.get("specValue", "")
                    if spec_name and spec_value:
                        spec_parts.append(f"{spec_name}:{spec_value}")
                        spec_val_parts.append(str(spec_value))
                elif isinstance(spec, str) and spec:
                    spec_val_parts.append(spec)
        if not spec_parts:
            for key in ["color", "size", "style", "material"]:
                val = sku_item.get(key)
                if val:
                    spec_parts.append(f"{key}:{val}")
                    spec_val_parts.append(str(val))

        # 缩略图
        thumb_url = ""
        thumb_data = sku_item.get("thumb") or sku_item.get("thumbUrl") or sku_item.get("imageUrl") or sku_item.get("img")
        if isinstance(thumb_data, dict):
            thumb_url = thumb_data.get("url") or thumb_data.get("src") or ""
        elif isinstance(thumb_data, str):
            thumb_url = thumb_data

        # 图片画廊
        gallery = []
        gallery_data = sku_item.get("gallery") or sku_item.get("images") or sku_item.get("imgList") or []
        if isinstance(gallery_data, list):
            for gid, g_item in enumerate(gallery_data, 1):
                if isinstance(g_item, dict):
                    g_url = g_item.get("url") or g_item.get("src") or g_item.get("imageUrl", "")
                    if g_url:
                        gallery.append({"id": gid, "url": str(g_url)})
                elif isinstance(g_item, str):
                    gallery.append({"id": gid, "url": g_item})
        if not gallery:
            gallery = self._parse_current_sku_gallery()
        if not thumb_url and gallery:
            thumb_url = gallery[0]["url"]

        # SKU URL
        sku_url = self.source_url
        sku_id_val = sku_item.get("skuId") or sku_item.get("id") or ""
        if sku_id_val:
            sku_url = f"{self.source_url}?sku_id={sku_id_val}"

        return {
            "skuId": str(sku_id),
            "goodsId": self.goods_id,
            "goodsName": goods_name,
            "thumbUrl": str(thumb_url),
            "currency": currency,
            "price": sku_price,
            "specKeyValues": ",".join(spec_parts),
            "specValues": ",".join(spec_val_parts),
            "skuGallery": gallery,
            "isskuGallery": 1,
            "url": sku_url,
        }

    def _build_skus_from_specs(self):
        """从规格组合生成 SKU 列表"""
        skus = []
        price_val, currency = self.parse_price()
        pdata = self._get_product_data()
        spec_groups_data = (
            pdata.get("specGroups") or pdata.get("groupList") or
            pdata.get("specList") or pdata.get("skcSpecList") or
            pdata.get("optionList") or []
        )
        all_specs = []
        if isinstance(spec_groups_data, list):
            for group in spec_groups_data:
                if not isinstance(group, dict):
                    continue
                group_name = str(
                    group.get("name") or group.get("specName") or
                    group.get("key") or group.get("attrName") or
                    group.get("optionName") or ""
                )
                group_values = (
                    group.get("values") or group.get("specValueList") or
                    group.get("options") or []
                )
                if isinstance(group_values, list) and group_name:
                    vals = []
                    for v in group_values:
                        if isinstance(v, dict):
                            val_name = str(
                                v.get("name") or v.get("specValue") or
                                v.get("value") or v.get("val") or
                                v.get("optionValue") or ""
                            )
                            if val_name:
                                vals.append({"name": val_name, "skuId": str(v.get("skuId", ""))})
                        elif isinstance(v, str):
                            vals.append({"name": v, "skuId": ""})
                    if vals:
                        all_specs.append({"name": group_name, "values": vals})
        if not all_specs:
            all_specs = self._parse_spec_groups_from_html()
        if all_specs:
            value_lists = [s["values"] for s in all_specs]
            names = [s["name"] for s in all_specs]
            for combo in itertools.product(*value_lists):
                spec_parts, spec_val_parts, sku_ids = [], [], []
                for i, val_info in enumerate(combo):
                    spec_parts.append(f"{names[i]}:{val_info['name']}")
                    spec_val_parts.append(val_info["name"])
                    if val_info.get("skuId"):
                        sku_ids.append(val_info["skuId"])
                sku_id = sku_ids[0] if sku_ids else f"{self.goods_id}_{len(skus)}"
                gallery = self._parse_current_sku_gallery()
                skus.append({
                    "skuId": sku_id,
                    "goodsId": self.goods_id,
                    "goodsName": self.parse_title(),
                    "thumbUrl": gallery[0]["url"] if gallery else "",
                    "currency": currency,
                    "price": safe_float(price_val),
                    "specKeyValues": ",".join(spec_parts),
                    "specValues": ",".join(spec_val_parts),
                    "skuGallery": gallery,
                    "isskuGallery": 1,
                    "url": self.source_url,
                })
        if skus:
            logger.info(f"从规格组合生成 {len(skus)} 个 SKU")
        return skus

    def _parse_spec_groups_from_html(self):
        """从 HTML 元素中解析规格组"""
        all_specs = []
        spec_sections = self.soup.select(
            '[class*="spec"], [class*="option"], [class*="variant"], '
            '[class*="selector"], [class*="attribute"]'
        )
        for section in spec_sections:
            label = section.select_one(
                '[class*="label"], [class*="title"], [class*="name"], label')
            group_name = safe_text(label).rstrip(":") if label else ""
            values = []
            for opt in section.select(
                '[class*="option"], [class*="value"], [class*="item"], '
                'button, [role="option"], li'
            ):
                val_text = safe_text(opt).strip()
                if val_text and val_text != group_name:
                    values.append({"name": val_text, "skuId": ""})
            if group_name and values:
                all_specs.append({"name": group_name, "values": values})
        return all_specs

    def _parse_current_specs(self):
        spec_parts, val_parts = [], []
        details = self.parse_product_details()
        for key in ["Color", "Size", "Style", "Material Type"]:
            val = details.get(key, "")
            if val:
                dim = key.lower().replace(" type", "").replace(" ", "_")
                spec_parts.append(f"{dim}:{val}")
                val_parts.append(val)
        return ",".join(spec_parts), ",".join(val_parts)

    def _parse_current_sku_gallery(self):
        gallery, img_id, seen = [], 1, set()
        for img in self.soup.select(
            '[class*="gallery"] img, [class*="swiper"] img, '
            '[class*="carousel"] img, [class*="slider"] img, '
            '[class*="mainImage"] img, [class*="productImg"] img, '
            'picture img'
        ):
            src = img.get("data-src") or img.get("src", "")
            if src and not src.startswith("data:") and "icon" not in src and "logo" not in src and "sprite" not in src and src not in seen:
                gallery.append({"id": img_id, "url": src})
                img_id += 1
                seen.add(src)
        return gallery

    # ---------- 主解析入口 ----------

    def parse(self):
        pdata = self._get_product_data()
        store = deep_get(self._extract_page_data(), "raw_data", "store") or {}

        # 如果内嵌数据为空，尝试从 URL 和 rawData store 中提取
        title = self.parse_title()
        if not title:
            title = self._parse_title_from_url()

        price_val, currency = self.parse_price()
        if price_val == "0.00":
            price_val, currency = self._parse_price_from_store(store)

        categories = self.parse_categories()
        images = self.parse_images()
        if not images:
            images = self._parse_images_from_url()
        brand = self.parse_brand()
        if not brand:
            brand = self._parse_brand_from_store(store)
        about = self.parse_about_this_item()
        details = self.parse_product_details()
        if not details:
            details = self._parse_details_from_store(store)
        desc = self.parse_product_description()

        # 检查 API 错误
        error = store.get("error") or store.get("webLayoutError") or {}
        if error:
            logger.warning(f"页面数据加载异常: {json.dumps(error, ensure_ascii=False)}")

        return {
            "goodsId": self.goods_id,
            "categories": categories,
            "images": images,
            "title": title,
            "price": price_val,
            "currency": currency,
            "brand": brand,
            "aboutThisItem": about,
            "productDetails": details,
            "productDescription": desc,
            "source_url": self.source_url,
            "mallId": store.get("mallId", ""),
            "platform_code": "temu",
            "skus": self.parse_skus(),
        }

    # ---------- URL/Fallback 数据提取 ----------

    def _parse_title_from_url(self):
        """从 URL slug 中提取商品标题"""
        parsed = urlparse(self.source_url)
        path = parsed.path
        m = re.search(r'/([^/]+)-g-\d+\.html', path)
        if m:
            slug = m.group(1)
            title = slug.replace('-', ' ').title()
            logger.info(f"从 URL 提取标题: {title}")
            return title
        return ""

    def _parse_images_from_url(self):
        """从 URL 查询参数中提取图片"""
        images = []
        parsed = urlparse(self.source_url)
        qs = parse_qs(parsed.query)
        for key in ['top_gallery_url', 'image_url', 'main_image']:
            vals = qs.get(key, [])
            if vals:
                from urllib.parse import unquote
                url_val = unquote(vals[0])
                if url_val.startswith('http'):
                    images.append(url_val)
                    logger.info(f"从 URL 提取图片: {url_val}")
                    return images
        return images

    def _parse_price_from_store(self, store):
        """从 rawData store 中提取价格信息"""
        local_info = store.get("localInfo") or store.get("webLayoutData", {}).get("commonData", {}).get("localInfo") or {}
        currency_code = local_info.get("currency", "USD")
        currency_map = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩", "CAD": "$"}
        currency = currency_map.get(currency_code, "$")

        # 尝试从 store 中找到价格
        price_data = store.get("price") or store.get("salePrice") or store.get("minPrice")
        if price_data is None:
            return "0.00", currency
        if isinstance(price_data, dict):
            amount = price_data.get("amount") or price_data.get("value") or 0
            try:
                price_float = float(amount) / 100
            except (ValueError, TypeError):
                price_float = safe_float(str(amount))
            return f"{price_float:.2f}", currency
        elif isinstance(price_data, (int, float)):
            try:
                price_float = float(price_data) / 100
            except (ValueError, TypeError):
                price_float = safe_float(str(price_data))
            return f"{price_float:.2f}", currency
        return "0.00", currency

    def _parse_brand_from_store(self, store):
        """从 rawData store 中提取品牌"""
        mall_data = store.get("mall") or {}
        if isinstance(mall_data, dict):
            for k in ["mallName", "mall_name", "name", "shopName"]:
                v = mall_data.get(k, "")
                if v:
                    return str(v)
        return ""

    def _parse_details_from_store(self, store):
        """从 rawData store 中提取商品详情"""
        details = {}
        # 从 formatSkuData 中提取规格信息
        sku_data = store.get("formatSkuData") or {}
        if isinstance(sku_data, dict):
            for k, v in sku_data.items():
                if v and k not in ("skuTypeValues", "skuInfos", "skuInfoMap"):
                    if isinstance(v, dict) and len(v) > 0:
                        details[f"sku_{k}"] = str(len(v))
        # 本地信息
        local_info = store.get("localInfo") or {}
        if isinstance(local_info, dict):
            for k in ["region", "language", "currency"]:
                v = local_info.get(k, "")
                if v:
                    details[k] = str(v)
        return details


# ========================= 主流程 =========================

def scrape_temu_product(url, output_dir="."):
    goods_id = extract_goods_id(url)
    logger.info(f"目标商品ID: {goods_id}")
    logger.info(f"cloudscraper 可用: {HAS_CLOUDSCRAPER}")
    logger.info(f"curl_cffi 可用: {HAS_CURL_CFFI}")

    html = fetch_page(url)

    parser = TemuProductParser(html, url)
    result = parser.parse()

    output_path = os.path.join(output_dir, f"{goods_id}.json")
    os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存到: {output_path}")
    return result


def main():
    if len(sys.argv) < 2:
        print("使用方法: python temu_scraper.py <temu_url> [output_dir]")
        print()
        print("示例:")
        print('  python temu_scraper.py "https://www.temu.com/xxxxx-g-123456.html"')
        print('  python temu_scraper.py "https://www.temu.com/xxxxx-g-123456.html" ./output')
        print()
        print("环境变量:")
        print("  TEMU_COOKIE    - 设置 Cookie")
        print("  HTTP_PROXY     - 设置 HTTP 代理")
        print("  HTTPS_PROXY    - 设置 HTTPS 代理")
        sys.exit(1)

    url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    try:
        result = scrape_temu_product(url, output_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error(f"爬取失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
