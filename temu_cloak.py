#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Temu Product Scraper - CloakBrowser Edition
============================================
基于 CloakBrowser 模拟真人浏览行为爬取 Temu 商品详情页。

依赖安装:
    pip install cloakbrowser beautifulsoup4 lxml
    playwright install chromium

使用方式:
    python temu_cloak.py "https://www.temu.com/xxxxx-g-123456.html"
    python temu_cloak.py "https://www.temu.com/xxxxx-g-123456.html" ./output
    python temu_cloak.py "https://www.temu.com/xxxxx-g-123456.html" ./output --visible

环境变量:
    HTTP_PROXY     - 设置 HTTP 代理
    HTTPS_PROXY    - 设置 HTTPS 代理
"""

import sys
import os
import re
import json
import time
import random
import logging
from urllib.parse import urlparse, parse_qs, unquote

from bs4 import BeautifulSoup, Tag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TemuCloakScraper")

REQUEST_TIMEOUT = 60000
PAGE_LOAD_TIMEOUT = 90000
MAX_RETRIES = 2

# ========================= 工具函数 =========================

def extract_goods_id(url):
    m = re.search(r"[-/]g[-/](\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[-/]d[-/](\d+)", url)
    if m:
        return m.group(1)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ["goods_id", "goodsId", "id"]:
        if key in qs:
            return qs[key][0]
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


def deep_get(data, *keys, default=None):
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


def human_delay(min_ms=200, max_ms=800):
    time.sleep(random.uniform(min_ms, max_ms) / 1000.0)


# ========================= 翻译功能 =========================

def _detect_has_non_chinese(text):
    """检测文本是否包含非中文内容（英文、日文等）"""
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


def _get_proxy_for_requests():
    """获取 requests 库可用的代理配置"""
    for var in ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy']:
        val = os.environ.get(var, '')
        if val:
            return {"http": val, "https": val}
    return None


def translate_to_chinese(text, cache=None, source='auto', proxy=None):
    """将文本翻译为中文，使用 Google Translate (free)"""
    if not text or not _detect_has_non_chinese(text):
        return text

    if cache is None:
        cache = {}
    text = text.strip()
    if text in cache:
        return cache[text]

    # 确保翻译请求走代理
    if proxy is None:
        proxy = _get_proxy_for_requests()

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
        try:
            time.sleep(2)
            from deep_translator import GoogleTranslator
            translator = GoogleTranslator(source=source, target='zh-CN', proxies=proxy)
            result = translator.translate(text)
            if result and result != text:
                cache[text] = result
                return result
        except Exception:
            pass

    return text


def translate_result(result):
    """返回一个全新翻译后的结果副本，原文替换为中文"""
    cache = {}
    proxy = _get_proxy_for_requests()
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
            cn[k] = translate_to_chinese(v, cache, proxy=proxy) if _detect_has_non_chinese(v) else v
            time.sleep(0.3)
        elif k == "brand":
            cn[k] = translate_to_chinese(v, cache, proxy=proxy) if _detect_has_non_chinese(v) else v
            time.sleep(0.3)
        elif k == "categories":
            cn[k] = [translate_to_chinese(c, cache, proxy=proxy) if _detect_has_non_chinese(c) else c
                      for c in (v or [])]
            for _ in (v or []):
                if _detect_has_non_chinese(_):
                    time.sleep(0.3)
        elif k == "aboutThisItem":
            cn[k] = [translate_to_chinese(i, cache, source='auto', proxy=proxy) if _detect_has_non_chinese(i) else i
                      for i in (v or [])]
            for _ in (v or []):
                if _detect_has_non_chinese(_):
                    time.sleep(0.3)
        elif k == "productDetails":
            cn[k] = {}
            for dk, dv in (v or {}).items():
                key_cn = translate_to_chinese(dk, cache, proxy=proxy) if _detect_has_non_chinese(dk) else dk
                val_cn = translate_to_chinese(str(dv), cache, proxy=proxy) if _detect_has_non_chinese(str(dv)) else str(dv)
                cn[k][key_cn] = val_cn
                time.sleep(0.3)
        elif k == "productDescription":
            cn[k] = translate_to_chinese(v, cache, source='auto', proxy=proxy) if _detect_has_non_chinese(v) else v
            time.sleep(0.3)
        elif k == "skus":
            cn[k] = []
            for sku in (v or []):
                sku_cn = {}
                for sk, sv in sku.items():
                    if sk == "goodsName" and _detect_has_non_chinese(sv):
                        sku_cn[sk] = translate_to_chinese(sv, cache, source='auto', proxy=proxy)
                        time.sleep(0.3)
                    else:
                        sku_cn[sk] = sv
                cn[k].append(sku_cn)
        else:
            cn[k] = v

    logger.info("翻译完成")
    return cn


# ========================= 浏览器启动 =========================

def create_browser(visible=False, proxy=None):
    from cloakbrowser import launch

    human_config = {
        "mouse_min_steps": 15,
        "mouse_max_steps": 50,
        "typing_delay": 80,
        "typing_delay_spread": 30,
        "scroll_delta_base": (80, 150),
        "scroll_pause_fast": (50, 120),
        "scroll_pause_slow": (100, 300),
        "idle_between_actions": True,
        "idle_between_duration": (0.3, 0.8),
    }

    logger.info("启动 CloakBrowser (humanize=True)...")
    browser = launch(
        headless=not visible,
        proxy=proxy,
        humanize=True,
        human_preset="default",
        human_config=human_config,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )
    return browser


# ========================= 页面抓取 =========================

def _visit_homepage(context):
    """先访问 Temu 首页，模拟真实用户入口"""
    page = context.new_page()
    logger.info("第1步: 访问 Temu 首页...")

    try:
        page.goto("https://www.temu.com/", wait_until="networkidle", timeout=60000)
        logger.info("首页加载完成")
    except Exception as e:
        logger.warning(f"首页加载异常(可忽略): {e}")

    # 首页浏览: 缓慢滚动几次
    logger.info("模拟首页浏览行为...")
    try:
        for i in range(random.randint(2, 3)):
            scroll_y = random.randint(300, 1200)
            page.evaluate(f"window.scrollTo({{top: {scroll_y}, behavior: 'smooth'}})")
            time.sleep(random.uniform(1.5, 3.5))
    except Exception:
        pass

    # 停留一段时间，模拟阅读
    stay = random.uniform(3, 6)
    logger.info(f"首页停留 {stay:.1f}s...")
    time.sleep(stay)

    # 回到顶部
    try:
        page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        time.sleep(random.uniform(0.5, 1.5))
    except Exception:
        pass

    page.close()
    logger.info("首页浏览完毕")


def fetch_product_page(browser, url, visible=False):
    """经过首页预热后，访问商品详情页"""

    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
    )

    # --- 阶段1: 逛首页 ---
    _visit_homepage(context)

    # 首页到商品页之间的过渡延迟 (模拟看推荐)
    interval = random.uniform(5, 12)
    logger.info(f"浏览推荐商品，等待 {interval:.1f}s 后进入商品页...")
    time.sleep(interval)

    # --- 阶段2: 打开商品页 ---
    logger.info(f"第2步: 打开商品详情页")

    page = context.new_page()

    try:
        page.goto(url, wait_until="networkidle", timeout=90000)
        logger.info("商品页加载完成")
    except Exception as e:
        logger.warning(f"商品页加载异常: {e}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

    # --- 阶段3: 模拟浏览商品详情 ---
    _browse_product_page(page)

    # --- 阶段4: 等待数据加载 ---
    success = _wait_for_product_data(page, visible=visible)
    if success:
        logger.info("商品数据加载成功")
    else:
        logger.warning("等待商品数据超时，尝试解析已有数据")

    # 额外缓冲
    time.sleep(random.uniform(2, 4))

    html = page.content()
    logger.info(f"获取页面内容，长度: {len(html)}")
    context.close()
    return html


def _browse_product_page(page):
    """低频率模拟浏览商品详情页"""
    logger.info("模拟商品页浏览...")

    # 看标题区域 (短停)
    time.sleep(random.uniform(1.5, 3))

    # 缓慢滚动浏览图片区
    try:
        for y in [400, 900, 1500, 2200]:
            page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'smooth'}})")
            time.sleep(random.uniform(2, 5))
    except Exception:
        pass

    # 在描述区域停留
    time.sleep(random.uniform(2, 4))

    # 慢慢回到顶部
    try:
        page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        time.sleep(random.uniform(1, 2))
    except Exception:
        pass

    logger.info("商品页浏览完毕")


def _wait_for_product_data(page, visible=False):
    """等待商品数据通过 API 加载到 window.rawData 中"""
    logger.info("等待商品数据加载...")
    start = time.time()
    max_wait = 90
    captcha_timeout = 180  # 验证码等待最长 3 分钟

    # 前15秒不做检查，让 API 自然完成
    initial_wait = random.uniform(10, 18)
    logger.info(f"静默等待 {initial_wait:.1f}s 让页面自然渲染...")
    time.sleep(initial_wait)

    captcha_detected = False
    captcha_start = 0

    while time.time() - start < max_wait:
        try:
            result = page.evaluate("""
                () => {
                    try {
                        const rd = window.rawData;
                        if (!rd || !rd.store) return {status: 'no_store'};
                        const store = rd.store;
                        const goods = store.goods;
                        if (goods && typeof goods === 'object' && Object.keys(goods).length > 0) {
                            return {status: 'ok', goodsKeys: Object.keys(goods).slice(0, 10)};
                        }
                        const error = store.error || store.webLayoutError;
                        if (error && error.errorCode) {
                            return {status: 'error', errorCode: error.errorCode, msg: error.message || error.errorMsg, token: error.verifyAuthToken || ''};
                        }
                        const h1 = document.querySelector('h1');
                        if (h1 && h1.textContent.trim().length > 5) {
                            return {status: 'dom_ready', title: h1.textContent.trim().slice(0, 80)};
                        }
                        const imgs = document.querySelectorAll('[class*="gallery"] img, [class*="product"] img');
                        if (imgs.length > 0) {
                            return {status: 'images_ready', count: imgs.length};
                        }
                        return {status: 'waiting', storeKeys: Object.keys(store).length};
                    } catch(e) {
                        return {status: 'exception', msg: e.message};
                    }
                }
            """)
            status = result.get("status", "unknown")

            if status == "ok":
                logger.info(f"商品数据已加载: {result.get('goodsKeys', [])}")
                return True
            elif status == "error":
                error_code = result.get("errorCode")
                if error_code == 54001 and visible:
                    if not captcha_detected:
                        captcha_detected = True
                        captcha_start = time.time()
                        logger.warning("=" * 60)
                        logger.warning("检测到人机验证 (54001)，请在浏览器窗口中手动完成验证")
                        logger.warning(f"Token: {result.get('token', 'N/A')}")
                        logger.warning("等待最长 3 分钟，验证通过后自动继续...")
                        logger.warning("=" * 60)
                    # 延长总等待时间，继续轮询
                    if time.time() - captcha_start < captcha_timeout:
                        elapsed = time.time() - captcha_start
                        if elapsed > 10 and int(elapsed) % 30 == 0:
                            logger.info(f"仍在等待验证... ({elapsed:.0f}s / {captcha_timeout}s)")
                        time.sleep(random.uniform(3, 6))
                        continue
                    else:
                        logger.warning("验证等待超时 (3分钟)")
                        return False
                else:
                    logger.warning(f"API 错误: code={error_code}, msg={result.get('msg')}")
                    if error_code != 54001 or not visible:
                        return False
            elif status in ("dom_ready", "images_ready"):
                logger.info(f"部分数据就绪: {status} - {result}")
                return True
            else:
                elapsed = time.time() - start
                if elapsed > 20:
                    logger.info(f"状态: {status} (已等 {elapsed:.0f}s)")
        except Exception as e:
            pass

        time.sleep(random.uniform(3, 6))

    logger.warning("等待超时")
    return False


# ========================= 页面解析 =========================

class CloakTemuParser:
    """解析 CloakBrowser 渲染后的页面"""

    def __init__(self, html, source_url):
        self.html = html
        self.soup = BeautifulSoup(html, "lxml")
        self.source_url = source_url
        self.goods_id = extract_goods_id(source_url)
        self._store = None

    def _get_store(self):
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

        # 备选: __INITIAL_STATE__ / __NEXT_DATA__
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

    def parse_title(self):
        store = self._get_store()
        goods = store.get("goods", {})
        for key in ["goodsName", "title", "goods_name", "name"]:
            val = goods.get(key, "") or store.get(key, "")
            if val:
                return str(val)

        # 从 DOM 提取
        h1 = self.soup.select_one("h1")
        if h1:
            return safe_text(h1)

        meta = self.soup.select_one('meta[property="og:title"]')
        if meta:
            return meta.get("content", "")

        # URL slug 回退
        return self._title_from_url()

    def parse_price(self):
        store = self._get_store()
        goods = store.get("goods", {})

        price_candidates = [
            # Temu store.goods 中的价格字段 (新版)
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
                # JP site: prices are in yen (no cent division needed)
                local_info = store.get("localInfo") or {}
                currency_code = local_info.get("currency", "")
                if currency_code in ("JPY", "KRW"):
                    return f"{float(val):.0f}", {"JPY": "¥", "KRW": "₩"}.get(currency_code, "$")
                try:
                    return f"{float(val) / 100:.2f}", "$"
                except (ValueError, TypeError):
                    return f"{safe_float(str(val)):.2f}", "$"
            elif isinstance(val, str):
                # 字符串价格: 如 "¥299"
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

        # DOM 提取
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
        store = self._get_store()
        goods = store.get("goods", {})

        # 优先从 mall 信息获取
        mall_data = store.get("mall", {})
        if isinstance(mall_data, dict):
            mall_detail = mall_data.get("mallData", mall_data)
            for key in ["mallName", "name"]:
                v = mall_detail.get(key, "")
                if v:
                    return str(v)

        # 从 saleInfo 获取
        sale_info = goods.get("saleInfo") or {}
        for key in ["mallName", "providedBy"]:
            v = sale_info.get(key, "")
            if v:
                return str(v)

        # 从 goods 直接字段
        for key in ["brand", "brandName", "brand_name"]:
            val = goods.get(key, "")
            if val:
                return str(val)

        brand_el = self.soup.select_one('[class*="brand"], [class*="store"], [class*="mall"]')
        if brand_el:
            return safe_text(brand_el)
        return ""

    def parse_categories(self):
        store = self._get_store()
        goods = store.get("goods", {})

        # 从 goods catId 字段构建分类路径（Temu 日本站没有 category 数组）
        cat_ids = {}
        for k in ["catId", "catId1", "catId2", "catId3", "catId4"]:
            v = goods.get(k)
            if v and isinstance(v, int):
                cat_ids[k] = v

        # 面包屑
        categories = []
        for a in self.soup.select('[class*="breadcrumb"] a, nav a'):
            text = safe_text(a)
            if text and text.lower() not in ("home", "temu", ""):
                if text not in categories:
                    categories.append(text)

        if categories:
            return categories

        # 回退 store 中的 category 数据
        cat_data = goods.get("category") or goods.get("categories") or []
        if isinstance(cat_data, list):
            for item in cat_data:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("catName", "")
                    if name and name not in categories:
                        categories.append(str(name))
                elif isinstance(item, str) and item not in categories:
                    categories.append(item)
        return categories

    def parse_images(self):
        store = self._get_store()
        goods = store.get("goods", {})

        gallery = goods.get("gallery") or goods.get("images") or goods.get("galleryList") or []
        images, seen = [], set()
        if isinstance(gallery, list):
            for item in gallery:
                if isinstance(item, dict):
                    url = item.get("url") or item.get("src") or item.get("imageUrl", "")
                    if url and url not in seen:
                        images.append(str(url))
                        seen.add(url)
                elif isinstance(item, str) and item not in seen:
                    images.append(item)
                    seen.add(item)

        if images:
            return images

        for img in self.soup.select('[class*="gallery"] img, [class*="productImg"] img, picture img'):
            src = img.get("data-src") or img.get("src", "")
            if src and not src.startswith("data:") and "icon" not in src and src not in seen:
                images.append(src)
                seen.add(src)

        if not images:
            images = self._images_from_url()
        return images

    def parse_about_this_item(self):
        store = self._get_store()
        goods = store.get("goods", {})

        bullets = []

        # 从 goodsProperty 提取描述
        goods_prop = goods.get("goodsProperty") or []
        if isinstance(goods_prop, list):
            for prop in goods_prop:
                if isinstance(prop, dict):
                    key = prop.get("key", "")
                    vals = prop.get("values") or []
                    if isinstance(vals, list) and vals:
                        bullets.append(f"{key}: {', '.join(str(v) for v in vals)}")

        # 从 saleInfo 提取销售信息
        sale_info = goods.get("saleInfo") or {}
        if isinstance(sale_info, dict):
            for sk in ["goodsSoldTip", "sideSalesTip"]:
                v = sale_info.get(sk, "")
                if v and v.strip():
                    bullets.append(str(v).strip().rstrip(","))

        # 从 extraProperty propertyList 提取富文本
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

        # 标准描述字段
        for src_name in ["description", "highlights", "features", "goodsDesc", "productDesc", "desc", "content"]:
            val = goods.get(src_name) or store.get(src_name)
            if val and isinstance(val, str) and val.strip() and val.strip() not in bullets:
                bullets.append(val.strip())

        # rows 中描述
        rows = goods.get("rows") or store.get("rows") or []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    desc = row.get("desc") or row.get("description") or row.get("text") or ""
                    if desc and desc not in bullets:
                        bullets.append(str(desc))

        if bullets:
            return bullets

        # DOM 回退
        for li in self.soup.select('[class*="description"] li, [class*="feature"] li, [class*="detail"] li'):
            text = safe_text(li)
            if text and len(text) > 5:
                bullets.append(text)
        return bullets

    def parse_product_details(self):
        store = self._get_store()
        goods = store.get("goods", {})

        details = {}

        # goodsProperty 格式: {"key": "材質", "values": ["プラスチック"]}
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

        # extraProperty.propertyList 中的 richText
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

        # saleInfo
        sale_info = goods.get("saleInfo") or {}
        if isinstance(sale_info, dict):
            for sk in ["goodsSoldTip", "sideSalesTip"]:
                v = sale_info.get(sk, "")
                if v and v.strip():
                    details["Sales"] = str(v).strip().rstrip(",")

        # mall 详细信息
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

        # 售出数量
        for f in ["soldQuantity", "soldCount"]:
            v = goods.get(f)
            if v and str(v).strip():
                details["Sold Quantity"] = str(v)

        # 本地信息
        local_info = store.get("localInfo") or {}
        if isinstance(local_info, dict):
            for k in ["region", "language", "currency"]:
                v = local_info.get(k, "")
                if v:
                    details[k] = str(v)

        return details

    def parse_product_description(self):
        store = self._get_store()
        goods = store.get("goods", {})
        for key in ["productDescription", "desc", "description", "content", "goodsDesc"]:
            val = goods.get(key, "")
            if val and isinstance(val, str) and len(val) > 10:
                return val
        desc_el = self.soup.select_one('[class*="productDesc"], [class*="goodsDesc"], [class*="detailContent"]')
        if desc_el:
            return safe_text(desc_el)
        return ""

    def parse_skus(self):
        store = self._get_store()
        goods = store.get("goods", {})
        sku_data = store.get("formatSkuData") or {}

        skus = []
        price_val, currency = self.parse_price()

        # 多个来源的 SKU 数据
        sku_list = (
            goods.get("skus") or goods.get("skuList") or
            goods.get("variants") or goods.get("variantList") or
            sku_data.get("skuInfos") or []
        )
        # skuInfos 可能是 dict (key→value) 或 list
        if isinstance(sku_data.get("skuInfos"), dict):
            sku_info_dict = sku_data.get("skuInfos", {})
            if not sku_list and sku_info_dict:
                sku_list = list(sku_info_dict.values())

        if isinstance(sku_list, list) and len(sku_list) > 0:
            for idx, item in enumerate(sku_list):
                if not isinstance(item, dict):
                    continue
                sku_id = str(item.get("skuId") or item.get("id") or item.get("sku_id") or f"{self.goods_id}_{idx}")
                sku_name = str(item.get("skuName") or item.get("name") or item.get("title") or self.parse_title())

                # 价格
                sku_price = None
                price_kwargs = {}
                price_field = item.get("price") or item.get("salePrice") or item.get("sale_price")
                if isinstance(price_field, dict):
                    amount = price_field.get("amount") or price_field.get("value") or 0
                    try:
                        sku_price = float(amount) / 100
                    except (ValueError, TypeError):
                        sku_price = safe_float(str(amount))
                    cc = price_field.get("currencyCode") or price_field.get("currency", "")
                    price_kwargs["currency"] = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "$"}.get(cc, currency)
                elif isinstance(price_field, (int, float)):
                    try:
                        sku_price = float(price_field) / 100
                    except (ValueError, TypeError):
                        sku_price = safe_float(str(price_field))
                if sku_price is None:
                    sku_price = safe_float(price_val) if price_val != "0.00" else 0.0

                # 规格
                spec_parts, spec_val_parts = [], []
                spec_sources = (
                    item.get("specList") or item.get("specs") or
                    item.get("attributes") or item.get("specValues") or []
                )
                if isinstance(spec_sources, list):
                    for spec in spec_sources:
                        if isinstance(spec, dict):
                            sn = spec.get("name") or spec.get("key") or spec.get("specName", "")
                            sv = spec.get("value") or spec.get("val") or spec.get("specValue", "")
                            if sn and sv:
                                spec_parts.append(f"{sn}:{sv}")
                                spec_val_parts.append(str(sv))
                        elif isinstance(spec, str):
                            spec_val_parts.append(spec)

                # 缩略图
                thumb = ""
                thumb_data = item.get("thumb") or item.get("thumbUrl") or item.get("imageUrl") or item.get("thumb_image")
                if isinstance(thumb_data, dict):
                    thumb = thumb_data.get("url") or thumb_data.get("src") or ""
                elif isinstance(thumb_data, str):
                    thumb = thumb_data
                if not thumb and self.parse_images():
                    thumb = self.parse_images()[0]

                # SKU 画廊
                gallery = []
                gallery_data = (
                    item.get("gallery") or item.get("images") or
                    item.get("imgList") or item.get("imageList") or []
                )
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

                # SKU 链接
                sku_url = item.get("url") or self.source_url

                skus.append({
                    "skuId": sku_id,
                    "goodsId": self.goods_id,
                    "goodsName": sku_name,
                    "thumbUrl": str(thumb),
                    "currency": price_kwargs.get("currency", currency),
                    "price": sku_price,
                    "specKeyValues": ",".join(spec_parts),
                    "specValues": ",".join(spec_val_parts),
                    "skuGallery": gallery,
                    "isskuGallery": 1 if gallery else 0,
                    "url": sku_url,
                })

        if not skus:
            main_images = self.parse_images()
            skus.append({
                "skuId": self.goods_id,
                "goodsId": self.goods_id,
                "goodsName": self.parse_title(),
                "thumbUrl": main_images[0] if main_images else "",
                "currency": currency,
                "price": safe_float(price_val) if price_val != "0.00" else 0.0,
                "specKeyValues": "",
                "specValues": "",
                "skuGallery": [],
                "isskuGallery": 0,
                "url": self.source_url,
            })

        return skus

    def _parse_current_sku_gallery(self):
        """从当前页面 DOM 提取 SKU 图片画廊"""
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

    # ---------- URL 回退方法 ----------

    def _title_from_url(self):
        path = urlparse(self.source_url).path
        m = re.search(r'/([^/]+)-g-\d+\.html', path)
        if m:
            title = m.group(1).replace("-", " ").title()
            logger.info(f"从 URL 提取标题: {title}")
            return title
        return ""

    def _images_from_url(self):
        parsed = urlparse(self.source_url)
        qs = parse_qs(parsed.query)
        for key in ['top_gallery_url', 'image_url', 'main_image']:
            vals = qs.get(key, [])
            if vals:
                url_val = unquote(vals[0])
                if url_val.startswith("http"):
                    return [url_val]
        return []

    # ---------- 主解析入口 ----------

    def parse(self):
        store = self._get_store()
        goods = store.get("goods", {})
        error = store.get("error") or store.get("webLayoutError") or {}

        # 调试：保存 goods 原始数据供分析
        try:
            debug_path = f"{self.goods_id}_debug_store.json"
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump({"goods": goods, "mall": store.get("mall", {}),
                            "displayModuleList": store.get("displayModuleStore", {}).get("displayModuleList", [])[:5]},
                           f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            pass

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


# ========================= 主流程 =========================

def scrape_temu_product(url, output_dir=".", visible=False, proxy=None, translate=True):
    goods_id = extract_goods_id(url)
    logger.info(f"目标商品ID: {goods_id}")

    browser = None
    try:
        browser = create_browser(visible=visible, proxy=proxy)
        html = fetch_product_page(browser, url, visible=visible)

        parser = CloakTemuParser(html, url)
        result = parser.parse()

        os.makedirs(output_dir, exist_ok=True)

        # 保存原始数据
        raw_path = os.path.join(output_dir, f"{goods_id}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"原始数据已保存到: {raw_path}")

        # 翻译并保存中文版
        if translate:
            cn_result = translate_result(result)
            cn_path = os.path.join(output_dir, f"{goods_id}_cn.json")
            with open(cn_path, "w", encoding="utf-8") as f:
                json.dump(cn_result, f, ensure_ascii=False, indent=2)
            logger.info(f"中文翻译已保存到: {cn_path}")
            return cn_result

        return result

    finally:
        if browser:
            try:
                browser.close()
                logger.info("浏览器已关闭")
            except Exception:
                pass


def _detect_proxy():
    """从环境变量自动检测代理地址 (兼容 Clash Verge)"""
    for var in ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy']:
        val = os.environ.get(var, '')
        if val:
            logger.info(f"检测到代理: {val}")
            return val
    return None


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Temu Product Scraper (CloakBrowser)")
    parser.add_argument("url", help="Temu 商品页面 URL")
    parser.add_argument("output_dir", nargs="?", default=".", help="输出目录")
    parser.add_argument("--visible", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--no-proxy", action="store_true", help="禁用代理")
    parser.add_argument("--no-translate", action="store_true", dest="no_translate", help="禁用外文翻译中文功能")
    args = parser.parse_args()

    # 自动检测代理
    proxy = None
    if not args.no_proxy:
        proxy = _detect_proxy()

    try:
        result = scrape_temu_product(
            args.url,
            output_dir=args.output_dir,
            visible=args.visible,
            proxy=proxy,
            translate=not args.no_translate,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2).encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8', errors='replace'))

        if result.get("price") == "0.00" or not result.get("title"):
            logger.warning("警告: 价格或标题为空，可能需要 Cookie")

    except Exception as e:
        logger.error(f"爬取失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
