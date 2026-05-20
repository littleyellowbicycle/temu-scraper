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
        self.html = html
        self.soup = BeautifulSoup(html, "lxml")
        self.source_url = source_url
        self.goods_id = extract_goods_id(source_url)
        self._store = None
        self._page_data = None

    # ---------- 页面内嵌数据提取 ----------

    def _get_store(self):
        """从 window.rawData 中提取 store 对象（带 goods keys 日志）"""
        if self._store is not None:
            return self._store
        idx = self.html.find("window.rawData")
        if idx >= 0:
            start = self.html.find("{", idx)
            if start >= 0:
                depth = 0
                for i in range(start, len(self.html)):
                    if self.html[i] == "{":
                        depth += 1
                    elif self.html[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                raw = self.html[start:i + 1]
                                raw = re.sub(r'\bundefined\b', 'null', raw)
                                data = json.loads(raw)
                                self._store = data.get("store", {})
                                goods = self._store.get("goods", {})
                                logger.info(f"提取 rawData.store: {len(self._store)} keys, goods: {len(goods)} keys")
                                if goods:
                                    logger.info(f"goods 字段: {list(goods.keys())[:30]}")
                                return self._store
                            except json.JSONDecodeError as e:
                                logger.warning(f"rawData 解析失败: {e}")
                            break
        for script in self.soup.select("script"):
            text = script.string or ""
            if not text:
                continue
            for prefix in ["__INITIAL_STATE__", "__NEXT_DATA__"]:
                m = re.search(
                    rf"window\.{prefix}\s*=\s*(\{{.*?\}})\s*;?\s*$",
                    text, re.DOTALL | re.MULTILINE,
                )
                if m:
                    try:
                        raw = re.sub(r'\bundefined\b', 'null', m.group(1))
                        self._store = json.loads(raw)
                        logger.info(f"提取 {prefix}")
                        return self._store
                    except json.JSONDecodeError:
                        continue
        self._store = {}
        return self._store

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
                                raw_store = page_data["raw_data"].get("store", {})
                                raw_goods = raw_store.get("goods", {})
                                if raw_goods:
                                    logger.info(f"rawData goods 字段: {list(raw_goods.keys())[:30]}")
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
        Temu 价格通常以分为单位存储（JPY/KRW 例外，不除以 100）
        """
        store = self._get_store()
        goods = store.get("goods", {})

        price_candidates = [
            "minOnSalePrice", "maxOnSalePrice", "minToMaxPriceStr",
            "minOnSalePriceStr", "salePriceRich", "minToMaxSalePriceRich",
            "salePrice", "price", "minPrice", "sale_price",
            "discountPrice", "appPrice",
        ]
        for field in price_candidates:
            val = goods.get(field)
            if val is None:
                continue
            if isinstance(val, dict):
                amount = val.get("amount") or val.get("value") or val.get("cent")
                currency_code = val.get("currencyCode") or val.get("currency", "")

                if not currency_code:
                    local_info = store.get("localInfo") or {}
                    currency_code = local_info.get("currency", "USD")

                if amount is not None:
                    try:
                        amount_f = float(amount)
                        if currency_code in ("JPY", "KRW"):
                            price_float = amount_f
                        else:
                            price_float = amount_f / 100
                    except (ValueError, TypeError):
                        price_float = safe_float(str(amount))
                    currency_map = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩", "CAD": "$"}
                    return f"{price_float:.0f}" if currency_code in ("JPY", "KRW") else f"{price_float:.2f}", currency_map.get(currency_code, "$")
            elif isinstance(val, (int, float)):
                local_info = store.get("localInfo") or {}
                currency_code = local_info.get("currency", "")
                if currency_code in ("JPY", "KRW"):
                    return f"{float(val):.0f}", {"JPY": "¥", "KRW": "₩"}.get(currency_code, "$")
                try:
                    return f"{float(val) / 100:.2f}", "$"
                except (ValueError, TypeError):
                    return f"{safe_float(str(val)):.2f}", "$"
            elif isinstance(val, str):
                currency = extract_currency_symbol(val)
                price_val = clean_price(val)
                if price_val and price_val != "0.00":
                    return price_val, currency

        # 尝试从 priceInfo 子结构获取
        price_info = goods.get("priceInfo") or goods.get("price_info") or {}
        if isinstance(price_info, dict):
            for field in ["salePrice", "price", "minPrice", "minOnSalePrice"]:
                val = price_info.get(field)
                if isinstance(val, (int, float)) and val > 0:
                    local_info = store.get("localInfo") or {}
                    currency = {"JPY": "¥", "USD": "$", "EUR": "€", "GBP": "£"}.get(local_info.get("currency", ""), "$")
                    try:
                        return f"{float(val) / 100:.2f}", currency
                    except (ValueError, TypeError):
                        return f"{safe_float(str(val)):.2f}", currency
                elif isinstance(val, str) and val:
                    return clean_price(val), extract_currency_symbol(val) or "$"

        # DOM 选择器
        for sel in [
            '[class*="price"] [class*="sale"]',
            '[class*="price"] [class*="discount"]',
            '[data-testid="product-price"]',
            '.price_current',
        ]:
            el = self.soup.select_one(sel)
            if el:
                text = safe_text(el)
                currency = extract_currency_symbol(text)
                price_val = clean_price(text)
                if price_val and price_val != "0.00":
                    return price_val, currency

        return "0.00", "$"

    def parse_brand(self):
        """解析品牌/店铺信息 — 优先 mall.mallData.mallName 和 saleInfo.mallName"""
        store = self._get_store()
        goods = store.get("goods", {})

        mall_data = store.get("mall", {})
        if isinstance(mall_data, dict):
            mall_detail = mall_data.get("mallData", mall_data)
            for key in ["mallName", "name"]:
                v = mall_detail.get(key, "")
                if v:
                    return str(v)

        sale_info = goods.get("saleInfo") or {}
        for key in ["mallName", "providedBy"]:
            v = sale_info.get(key, "")
            if v:
                return str(v)

        for key in ["brand", "brandName", "brand_name", "mallName", "shopName", "shop_name"]:
            val = goods.get(key, "")
            if val:
                return str(val)

        brand_el = self.soup.select_one(
            '[class*="brand"], [class*="store"], [class*="shop"], [class*="mall"]')
        if brand_el:
            text = safe_text(brand_el)
            if text:
                return text
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
        """解析商品要点 — goodsProperty、saleInfo、extraProperty"""
        store = self._get_store()
        goods = store.get("goods", {})

        bullets = []

        goods_prop = goods.get("goodsProperty") or []
        if isinstance(goods_prop, list):
            for prop in goods_prop:
                if isinstance(prop, dict):
                    key = prop.get("key", "")
                    vals = prop.get("values") or []
                    if isinstance(vals, list) and vals:
                        bullets.append(f"{key}: {', '.join(str(v) for v in vals)}")

        sale_info = goods.get("saleInfo") or {}
        if isinstance(sale_info, dict):
            for sk in ["goodsSoldTip", "sideSalesTip"]:
                v = sale_info.get(sk, "")
                if v and v.strip():
                    bullets.append(str(v).strip().rstrip(","))

        extra_prop = goods.get("extraProperty") or {}
        if isinstance(extra_prop, dict):
            prop_list = extra_prop.get("propertyList") or []
            for item in prop_list:
                if isinstance(item, dict):
                    rich = item.get("richText") or {}
                    texts = rich.get("textRich") or []
                    for t in texts:
                        if isinstance(t, dict) and t.get("type") == 0:
                            v = t.get("value", "")
                            if v and v not in bullets:
                                bullets.append(str(v))

        for src_name in ["description", "highlights", "features", "goodsDesc", "productDesc", "desc", "content"]:
            val = goods.get(src_name) or store.get(src_name)
            if val and isinstance(val, str) and val.strip() and val.strip() not in bullets:
                bullets.append(val.strip())

        rows = goods.get("rows") or store.get("rows") or []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    desc = row.get("desc") or row.get("description") or row.get("text") or ""
                    if desc and desc not in bullets:
                        bullets.append(str(desc))

        if bullets:
            return bullets

        for li in self.soup.select(
            '[class*="description"] li, [class*="feature"] li, '
            '[class*="detail"] li'
        ):
            text = safe_text(li)
            if text and len(text) > 5:
                bullets.append(text)
        return bullets

    def parse_product_description(self):
        """解析商品描述 — 检查 goods 字段，回退到 DOM"""
        store = self._get_store()
        goods = store.get("goods", {})
        for key in ["productDescription", "desc", "description", "content", "goodsDesc"]:
            val = goods.get(key, "")
            if val and isinstance(val, str) and len(val) > 10:
                return val
        desc_el = self.soup.select_one(
            '[class*="productDesc"], [class*="goodsDesc"], [class*="detailContent"]')
        if desc_el:
            return safe_text(desc_el)
        return ""

    def parse_product_details(self):
        """解析商品详细信息 — goodsProperty、extraProperty、saleInfo、mall"""
        store = self._get_store()
        goods = store.get("goods", {})

        details = {}

        goods_prop = goods.get("goodsProperty") or []
        if isinstance(goods_prop, list):
            for prop in goods_prop:
                if isinstance(prop, dict):
                    key = prop.get("key", "")
                    vals = prop.get("values") or prop.get("value") or []
                    if isinstance(vals, list) and vals:
                        details[str(key)] = ", ".join(str(v) for v in vals)
                    elif isinstance(vals, str) and vals:
                        details[str(key)] = vals

        extra_prop = goods.get("extraProperty") or {}
        if isinstance(extra_prop, dict):
            prop_list = extra_prop.get("propertyList") or []
            for item in prop_list:
                if isinstance(item, dict):
                    rich = item.get("richText") or {}
                    texts = rich.get("textRich") or []
                    label_parts, value_parts = [], []
                    for t in texts:
                        if isinstance(t, dict):
                            ttype = t.get("type", -1)
                            val = t.get("value", "")
                            if ttype == 0 and val:
                                label_parts.append(val)
                            elif val and ttype != 0:
                                value_parts.append(val)
                    if label_parts and value_parts:
                        details[" ".join(label_parts)] = " ".join(value_parts)

        sale_info = goods.get("saleInfo") or {}
        if isinstance(sale_info, dict):
            for sk in ["goodsSoldTip", "sideSalesTip"]:
                v = sale_info.get(sk, "")
                if v and v.strip():
                    details["Sales"] = str(v).strip().rstrip(",")

        mall_data = store.get("mall", {})
        if isinstance(mall_data, dict):
            mall_detail = mall_data.get("mallData", mall_data)
            for mk, ml in [("mallName", "Store Name"), ("mallStarStr", "Store Rating"),
                           ("reviewNumStr", "Store Reviews"), ("goodsNum", "Store Products"),
                           ("followerNumUnit", "Store Followers")]:
                v = mall_detail.get(mk)
                if v:
                    if isinstance(v, list):
                        v = " ".join(str(x) for x in v)
                    if str(v).strip():
                        details[ml] = str(v)

        for f in ["soldQuantity", "soldCount"]:
            v = goods.get(f)
            if v and str(v).strip():
                details["Sold Quantity"] = str(v)

        local_info = store.get("localInfo") or {}
        if isinstance(local_info, dict):
            for k in ["region", "language", "currency"]:
                v = local_info.get(k, "")
                if v:
                    details[k] = str(v)

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
        store = self._get_store()
        goods = store.get("goods", {})
        error = store.get("error") or store.get("webLayoutError") or {}

        if error:
            logger.warning(f"页面数据加载异常: {json.dumps(error, ensure_ascii=False)}")

        mall_id = goods.get("mallId") or store.get("mallId") or ""

        return {
            "goodsId": self.goods_id,
            "categories": self.parse_categories(),
            "images": self.parse_images(),
            "title": self.parse_title(),
            "price": self.parse_price()[0],
            "currency": self.parse_price()[1],
            "brand": self.parse_brand(),
            "aboutThisItem": self.parse_about_this_item(),
            "productDetails": self.parse_product_details(),
            "productDescription": self.parse_product_description(),
            "source_url": self.source_url,
            "mallId": mall_id,
            "platform_code": "temu",
            "skus": self.parse_skus(),
            "_api_error": error if error else None,
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


# ========================= 主流程 =========================

def _detect_has_non_chinese(text):
    if not text:
        return False
    non_cn = 0
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            continue
        if ch.isspace() or ch.isdigit() or ch in '.,;:!?()-/–—・·':
            continue
        non_cn += 1
    return non_cn > len(text) * 0.3


def translate_to_chinese(text, cache=None, source='auto'):
    if not text or not _detect_has_non_chinese(text):
        return text
    if cache is None:
        cache = {}
    text = text.strip()
    if text in cache:
        return cache[text]
    proxy = None
    for var in ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy']:
        val = os.environ.get(var, '')
        if val:
            proxy = {"http": val, "https": val}
            break
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source=source, target='zh-CN', proxies=proxy)
        result = translator.translate(text)
        if result and result != text:
            cache[text] = result
            logger.info(f"翻译: {text[:50]}... → {result[:50]}...")
            return result
    except Exception as e:
        logger.warning(f"翻译失败 ({text[:30]}...): {e}")
    return text


def translate_result(result):
    """返回一个全新翻译后的结果副本，原文替换为中文"""
    cache = {}
    proxy = None
    for var in ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy']:
        val = os.environ.get(var, '')
        if val:
            proxy = {"http": val, "https": val}
            break
    logger.info("开始翻译外文字段为中文...")

    cn = {}
    _skip_keys = {"source_url", "goodsId", "_api_error", "mallId", "platform_code"}
    _image_keys = {"images", "thumbUrl", "skuGallery"}
    _copy_keys = {"price", "currency"}

    for k, v in result.items():
        if k in _skip_keys:
            cn[k] = v
        elif k in _image_keys:
            cn[k] = v
        elif k in _copy_keys:
            cn[k] = v
        elif k == "title":
            cn[k] = translate_to_chinese(v, cache) if _detect_has_non_chinese(v) else v
            time.sleep(0.3)
        elif k == "brand":
            cn[k] = translate_to_chinese(v, cache) if _detect_has_non_chinese(v) else v
            time.sleep(0.3)
        elif k == "categories":
            cn[k] = [translate_to_chinese(c, cache) if _detect_has_non_chinese(c) else c
                     for c in (v or [])]
        elif k == "aboutThisItem":
            cn[k] = [translate_to_chinese(i, cache, source='auto') if _detect_has_non_chinese(i) else i
                     for i in (v or [])]
        elif k == "productDetails":
            cn[k] = {(
                translate_to_chinese(dk, cache) if _detect_has_non_chinese(dk) else dk
            ): (
                translate_to_chinese(str(dv), cache) if _detect_has_non_chinese(str(dv)) else str(dv)
            ) for dk, dv in (v or {}).items()}
        elif k == "productDescription":
            cn[k] = translate_to_chinese(v, cache, source='auto') if _detect_has_non_chinese(v) else v
        elif k == "skus":
            cn[k] = []
            for sku in (v or []):
                sku_cn = {}
                for sk, sv in sku.items():
                    if sk == "goodsName" and _detect_has_non_chinese(sv):
                        sku_cn[sk] = translate_to_chinese(sv, cache, source='auto')
                    else:
                        sku_cn[sk] = sv
                cn[k].append(sku_cn)
        else:
            cn[k] = v

    logger.info("翻译完成")
    return cn


def scrape_temu_product(url, output_dir="."):
    goods_id = extract_goods_id(url)
    logger.info(f"目标商品ID: {goods_id}")
    logger.info(f"cloudscraper 可用: {HAS_CLOUDSCRAPER}")
    logger.info(f"curl_cffi 可用: {HAS_CURL_CFFI}")

    html = fetch_page(url)

    parser = TemuProductParser(html, url)
    result = parser.parse()

    os.makedirs(output_dir, exist_ok=True)

    # 保存原始数据
    raw_path = os.path.join(output_dir, f"{goods_id}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"原始数据已保存到: {raw_path}")

    # 翻译并保存中文版
    cn_result = translate_result(result)
    cn_path = os.path.join(output_dir, f"{goods_id}_cn.json")
    with open(cn_path, "w", encoding="utf-8") as f:
        json.dump(cn_result, f, ensure_ascii=False, indent=2)
    logger.info(f"中文翻译已保存到: {cn_path}")

    return cn_result


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
        print(json.dumps(result, ensure_ascii=False, indent=2).encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8', errors='replace'))
    except Exception as e:
        logger.error(f"爬取失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
