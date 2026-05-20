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

def fetch_product_page(browser, url):
    logger.info(f"打开商品页面: {url}")

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

    page = context.new_page()

    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        logger.info(f"初始状态: {resp.status if resp else 'N/A'}")
    except Exception as e:
        logger.warning(f"初始导航异常: {e}")

    # 模拟真人浏览行为
    _simulate_human_browsing(page)

    # 等待 API 请求完成并数据加载
    success = _wait_for_product_data(page)
    if success:
        logger.info("商品数据加载成功")
    else:
        logger.warning("等待商品数据超时，将尝试解析已有数据")

    # 额外等待一下确保所有异步请求完成
    time.sleep(3)

    html = page.content()
    logger.info(f"获取页面内容，长度: {len(html)}")
    context.close()
    return html


def _simulate_human_browsing(page):
    """模拟真人浏览行为"""
    try:
        # 1. 随机滚动
        for _ in range(random.randint(3, 5)):
            scroll_y = random.randint(200, 1500)
            page.evaluate(f"window.scrollTo({{top: {scroll_y}, behavior: 'smooth'}})")
            human_delay(300, 1200)
    except Exception:
        pass

    try:
        # 2. 回到顶部
        page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        human_delay(400, 1000)
    except Exception:
        pass

    try:
        # 3. 模拟鼠标悬停在图片区域
        page.evaluate("""
            const el = document.querySelector('img');
            if (el) {
                el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
            }
        """)
        human_delay(200, 600)
    except Exception:
        pass

    try:
        # 4. 缓慢滚动浏览
        for y in range(0, 2000, random.randint(100, 300)):
            page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'smooth'}})")
            human_delay(50, 150)
    except Exception:
        pass


def _wait_for_product_data(page):
    """等待商品数据通过 API 加载到 window.rawData 中"""
    logger.info("等待商品数据加载...")
    start = time.time()
    max_wait = 60

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
                            return {status: 'error', errorCode: error.errorCode, msg: error.message || error.errorMsg};
                        }
                        // 检查是否有 h1/title 元素
                        const h1 = document.querySelector('h1');
                        if (h1 && h1.textContent.trim().length > 5) {
                            return {status: 'dom_ready', title: h1.textContent.trim().slice(0, 80)};
                        }
                        // 检查是否有图片
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
                logger.warning(f"API 错误: code={result.get('errorCode')}, msg={result.get('msg')}")
                return False
            elif status in ("dom_ready", "images_ready"):
                logger.info(f"部分数据就绪: {status} - {result}")
                return True
            else:
                if time.time() - start > 10:
                    logger.debug(f"状态: {status}, elapsed: {time.time()-start:.0f}s")
        except Exception as e:
            logger.debug(f"JS 检查异常: {e}")

        time.sleep(2)

    logger.warning("等待超时")
    return False


def _has_product_data(html):
    """检查 HTML 中是否包含商品数据"""
    if "goodsId" in html and "goods_id" in html.lower():
        return True
    if '<h1' in html.lower():
        return True
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
                                logger.info(f"提取 rawData.store: {len(self._store)} keys")
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
            "salePrice", "price", "minPrice", "sale_price",
            "discountPrice", "appPrice",
        ]
        for field in price_candidates:
            val = goods.get(field)
            if val is None:
                continue
            if isinstance(val, dict):
                amount = val.get("amount") or val.get("value") or val.get("cent")
                currency_code = val.get("currencyCode") or val.get("currency", "USD")
                if amount is not None:
                    try:
                        price_float = float(amount) / 100
                    except (ValueError, TypeError):
                        price_float = safe_float(str(amount))
                    currency_map = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩", "CAD": "$"}
                    return f"{price_float:.2f}", currency_map.get(currency_code, "$")
            elif isinstance(val, (int, float)):
                try:
                    return f"{float(val) / 100:.2f}", "$"
                except (ValueError, TypeError):
                    return f"{safe_float(str(val)):.2f}", "$"
            elif isinstance(val, str):
                currency = extract_currency_symbol(val)
                price_val = clean_price(val)
                if price_val and price_val != "0.00":
                    return price_val, currency

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
        mall_data = store.get("mall", {})

        for key in ["brand", "brandName", "brand_name"]:
            val = goods.get(key, "")
            if val:
                return str(val)
        for key in ["mallName", "shopName", "name"]:
            val = mall_data.get(key, "")
            if val:
                return str(val)

        brand_el = self.soup.select_one('[class*="brand"], [class*="store"], [class*="mall"]')
        if brand_el:
            text = safe_text(brand_el)
            if text:
                return text
        return ""

    def parse_categories(self):
        store = self._get_store()
        goods = store.get("goods", {})
        cat_data = goods.get("category") or goods.get("categories") or goods.get("catPath", [])

        categories = []
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
        for a in self.soup.select('[class*="breadcrumb"] a, nav a'):
            text = safe_text(a)
            if text and text.lower() not in ("home", "temu", ""):
                if text not in categories:
                    categories.append(text)
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
        desc_list = goods.get("description") or goods.get("highlights") or goods.get("features") or []
        bullets = []
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

        if not bullets:
            for li in self.soup.select('[class*="description"] li, [class*="feature"] li, [class*="detail"] li'):
                text = safe_text(li)
                if text and len(text) > 5:
                    bullets.append(text)
        return bullets

    def parse_product_details(self):
        store = self._get_store()
        goods = store.get("goods", {})
        details = {}

        spec_list = goods.get("specs") or goods.get("specifications") or goods.get("attrList") or []
        if isinstance(spec_list, list):
            for item in spec_list:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("key") or item.get("attrName") or item.get("label", "")
                    val = item.get("value") or item.get("val") or item.get("attrValue") or item.get("content", "")
                    if key and val:
                        details[str(key)] = str(val)

        # 本地信息
        local_info = store.get("localInfo") or {}
        if isinstance(local_info, dict):
            for k in ["region", "language", "currency"]:
                v = local_info.get(k, "")
                if v:
                    details[f"local_{k}"] = str(v)

        return details

    def parse_skus(self):
        store = self._get_store()
        goods = store.get("goods", {})

        skus = []
        price_val, currency = self.parse_price()

        sku_list = (
            goods.get("skus") or goods.get("skuList") or
            goods.get("variants") or goods.get("variantList") or []
        )

        if isinstance(sku_list, list) and len(sku_list) > 0:
            for idx, item in enumerate(sku_list):
                if not isinstance(item, dict):
                    continue
                sku_id = str(item.get("skuId") or item.get("id") or f"{self.goods_id}_{idx}")
                sku_name = str(item.get("skuName") or item.get("name") or item.get("title") or self.parse_title())

                sku_price = safe_float(price_val) if price_val != "0.00" else None
                price_field = item.get("price") or item.get("salePrice") or item.get("sale_price")
                if isinstance(price_field, dict):
                    amount = price_field.get("amount") or price_field.get("value") or 0
                    try:
                        sku_price = float(amount) / 100
                    except (ValueError, TypeError):
                        sku_price = safe_float(str(amount))
                elif isinstance(price_field, (int, float)):
                    try:
                        sku_price = float(price_field) / 100
                    except (ValueError, TypeError):
                        sku_price = safe_float(str(price_field))
                elif isinstance(price_field, str):
                    sku_price = safe_float(clean_price(price_field))
                if sku_price is None:
                    sku_price = safe_float(price_val)

                spec_parts, spec_val_parts = [], []
                spec_list = (
                    item.get("specList") or item.get("specs") or
                    item.get("attributes") or item.get("specValues") or []
                )
                if isinstance(spec_list, list):
                    for spec in spec_list:
                        if isinstance(spec, dict):
                            sn = spec.get("name") or spec.get("key") or spec.get("specName", "")
                            sv = spec.get("value") or spec.get("val") or spec.get("specValue", "")
                            if sn and sv:
                                spec_parts.append(f"{sn}:{sv}")
                                spec_val_parts.append(str(sv))
                        elif isinstance(spec, str):
                            spec_val_parts.append(spec)

                thumb = ""
                thumb_data = item.get("thumb") or item.get("thumbUrl") or item.get("imageUrl")
                if isinstance(thumb_data, dict):
                    thumb = thumb_data.get("url") or ""
                elif isinstance(thumb_data, str):
                    thumb = thumb_data

                gallery = []
                gallery_data = item.get("gallery") or item.get("images") or item.get("imgList") or []
                if isinstance(gallery_data, list):
                    for gid, g_item in enumerate(gallery_data, 1):
                        if isinstance(g_item, dict):
                            g_url = g_item.get("url") or g_item.get("src", "")
                            if g_url:
                                gallery.append({"id": gid, "url": str(g_url)})
                        elif isinstance(g_item, str):
                            gallery.append({"id": gid, "url": g_item})

                skus.append({
                    "skuId": sku_id,
                    "goodsId": self.goods_id,
                    "goodsName": sku_name,
                    "thumbUrl": str(thumb),
                    "currency": currency,
                    "price": sku_price,
                    "specKeyValues": ",".join(spec_parts),
                    "specValues": ",".join(spec_val_parts),
                    "skuGallery": gallery,
                    "isskuGallery": 1,
                    "url": self.source_url,
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
                "isskuGallery": 1,
                "url": self.source_url,
            })

        return skus

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
        price_val, currency = self.parse_price()
        title = self.parse_title()
        images = self.parse_images()
        details = self.parse_product_details()

        store = self._get_store()
        error = store.get("error") or store.get("webLayoutError") or {}

        return {
            "goodsId": self.goods_id,
            "categories": self.parse_categories(),
            "images": images,
            "title": title,
            "price": price_val,
            "currency": currency,
            "brand": self.parse_brand(),
            "aboutThisItem": self.parse_about_this_item(),
            "productDetails": details,
            "productDescription": "",
            "source_url": self.source_url,
            "mallId": store.get("mallId", ""),
            "platform_code": "temu",
            "skus": self.parse_skus(),
            "_api_error": error if error else None,
        }


# ========================= 主流程 =========================

def scrape_temu_product(url, output_dir=".", visible=False, proxy=None):
    goods_id = extract_goods_id(url)
    logger.info(f"目标商品ID: {goods_id}")

    browser = None
    try:
        browser = create_browser(visible=visible, proxy=proxy)
        html = fetch_product_page(browser, url)

        parser = CloakTemuParser(html, url)
        result = parser.parse()

        output_path = os.path.join(output_dir, f"{goods_id}.json")
        os.makedirs(output_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"结果已保存到: {output_path}")

        return result

    finally:
        if browser:
            try:
                browser.close()
                logger.info("浏览器已关闭")
            except Exception:
                pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Temu Product Scraper (CloakBrowser)")
    parser.add_argument("url", help="Temu 商品页面 URL")
    parser.add_argument("output_dir", nargs="?", default=".", help="输出目录")
    parser.add_argument("--visible", action="store_true", help="显示浏览器窗口")
    args = parser.parse_args()

    try:
        result = scrape_temu_product(
            args.url,
            output_dir=args.output_dir,
            visible=args.visible,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

        # 数据质量检查
        if result.get("price") == "0.00" or not result.get("title"):
            logger.warning("警告: 价格或标题为空，可能需要代理或 Cookie")

    except Exception as e:
        logger.error(f"爬取失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
