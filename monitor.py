#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京东商品降价监控脚本 (monitor.py)
================================
 
功能：
  1. 从 products.txt 读取京东商品链接
  2. 定时查询每个商品的当前价格（京东官方价格接口）
  3. 价格存入 SQLite 数据库，保留完整历史记录
  4. 与上次价格对比，降价时通过 Server酱 发微信通知
  5. SendKey 从环境变量读取，不写死在代码里
 
用法：
  首次测试（只跑一次）：  python monitor.py --once
  持续监控（循环运行）：  python monitor.py
  自定义间隔（秒）：      python monitor.py --interval 1800
  测试通知是否能发：      python monitor.py --test-notify
"""
 
import os
import re
import sys
import time
import sqlite3
import requests
from datetime import datetime
 
# ---- 可选：自动加载 .env 文件 ----
# 如果安装了 python-dotenv，会自动读取当前目录下的 .env 文件
# 没装也没关系，会直接从系统环境变量读取
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
 
 
# ================================================================
#  配置区（根据自己的需要修改这些参数）
# ================================================================
 
# 商品列表文件名（每行一个京东商品链接）
PRODUCTS_FILE = "products.txt"
 
# 数据库文件名（SQLite，自动生成）
DB_FILE = "jd_monitor.db"
 
# 检查间隔（秒）。3600 = 1小时，1800 = 半小时
CHECK_INTERVAL = 3600
 
# 京东价格接口（不用改）
# 调用时会把 { } 替换成商品ID
JD_PRICE_API = "https://p.3.cn/prices/mgets?skuIds=J_{}"
 
# Server酱 接口（不用改）
# 调用时会把 { } 替换成你的 SendKey
SERVERCHAN_API = "https://sctapi.ftqq.com/{}.send"
 
# 请求头（模拟浏览器访问，防止被京东屏蔽）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
 
 
# ================================================================
#  工具函数
# ================================================================
 
def get_sendkey():
    """
    从环境变量读取 Server酱 的 SendKey
 
    环境变量名：SC_SENDKEY
    你可以在 .env 文件里写 SC_SENDKEY=你的key
    也可以在命令行执行 export SC_SENDKEY=你的key
 
    返回：SendKey 字符串，如果没配置返回 None
    """
    # os.environ.get() 从环境变量读取，找不到就返回默认值 ""
    key = os.environ.get("SC_SENDKEY", "").strip()
 
    if not key:
        print("\n⚠️  没有配置 SendKey！降价时无法发通知。")
        print("   请设置环境变量 SC_SENDKEY，方法：")
        print("   1. 创建 .env 文件，写入：SC_SENDKEY=你的key")
        print("   2. 或者命令行执行：export SC_SENDKEY=你的key")
        return None
 
    return key
 
 
def load_products():
    """
    从 products.txt 读取商品列表
    
    文件格式（每行一个商品）：
      方式1 - 直接写链接：https://item.jd.com/100012043978.html
      方式2 - 名称和链接用竖线分隔：我的手机|https://item.jd.com/100012043978.html
 
    以 # 开头的行是注释，会被忽略
    空行也会被忽略
 
    返回：列表，每项是一个元组 (商品名称, 商品链接)
          如果用户没写名称，商品名称就是空字符串 ""
    """
    # 检查文件是否存在
    if not os.path.exists(PRODUCTS_FILE):
        print(f"❌ 找不到商品列表文件：{PRODUCTS_FILE}")
        print(f"   请先创建该文件，每行写一个京东商品链接")
        return []
 
    products = []
 
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()           # 去掉首尾空格和换行符
            if not line:                 # 空行跳过
                continue
            if line.startswith("#"):      # 注释行跳过
                continue
 
            # 检查是否有竖线分隔的名称
            if "|" in line:
                # split("|", 1) 表示只按第一个竖线分割
                name, url = line.split("|", 1)
                products.append((name.strip(), url.strip()))
            else:
                # 没有名称，只存链接
                products.append(("", line))
 
    print(f"📦 已加载 {len(products)} 个商品")
    return products
 
 
def extract_sku_id(url):
    """
    从京东商品链接中提取商品ID（也叫 SKU ID）
 
    例如：
      https://item.jd.com/100012043978.html  →  "100012043978"
      100012043978                            →  "100012043978"
 
    参数：url - 京东商品链接或纯数字ID
    返回：商品ID字符串，提取失败返回 None
    """
    # 方法1：用正则从完整链接中提取数字ID
    # 正则含义：匹配 item.jd.com/ 后面的连续数字
    match = re.search(r'item\.jd\.com/(\d+)', url)
    if match:
        return match.group(1)   # group(1) 返回括号里匹配到的内容
 
    # 方法2：如果用户直接填了纯数字，也接受
    if re.fullmatch(r'\d+', url):
        return url
 
    # 都不匹配，说明链接格式不对
    return None
 
 
def get_display_name(name, sku_id):
    """
    获取商品的显示名称
    如果用户在 products.txt 里写了名称，就用那个
    如果没写，就用 "商品{sku_id}" 代替
    """
    if name:
        return name
    return f"商品{sku_id}"
 
 
def get_jd_price(sku_id):
    """
    调用京东价格接口，获取商品当前价格
 
    接口地址：https://p.3.cn/prices/mgets?skuIds=J_{商品ID}
    返回的 JSON 类似：
      [{"id":"J_100012043978", "p":"999.00", "m":"1299.00", "op":"999.00"}]
    其中 "p" 就是当前价格
 
    参数：sku_id - 商品ID
    返回：价格（浮点数），获取失败返回 None
    """
    # 拼接完整的接口地址
    url = JD_PRICE_API.format(sku_id)
 
    try:
        # 发送 GET 请求，超时10秒
        resp = requests.get(url, headers=HEADERS, timeout=10)
        # 设置编码为 utf-8
        resp.encoding = "utf-8"
        # 把返回的 JSON 字符串解析成 Python 字典/列表
        data = resp.json()
 
        # 检查返回的数据是否正常
        # data 是一个列表，第一个元素是包含价格的字典
        if data and len(data) > 0 and "p" in data[0]:
            # data[0]["p"] 是价格字符串，如 "999.00"
            # float() 把字符串转成浮点数
            price = float(data[0]["p"])
            return price
        else:
            print(f"   ⚠️  商品 {sku_id}：接口未返回价格数据")
            return None
 
    except requests.RequestException as e:
        # 网络请求出错（断网、超时等）
        print(f"   ⚠️  商品 {sku_id}：网络请求失败 - {e}")
        return None
    except (ValueError, KeyError, IndexError) as e:
        # JSON 解析出错或字段不存在
        print(f"   ⚠️  商品 {sku_id}：数据解析失败 - {e}")
        return None
 
 
# ================================================================
#  数据库操作（SQLite）
# ================================================================
 
def init_db():
    """
    初始化 SQLite 数据库
 
    创建一张表叫 price_history，用来记录每次查到的价格
    字段说明：
      id         - 自增主键（每条记录自动编号）
      sku_id     - 商品ID
      url        - 商品链接
      price      - 价格
      checked_at - 检查时间（文本格式，如 "2024-01-15 14:30:00"）
 
    如果表已经存在，不会重复创建（IF NOT EXISTS）
    """
    # 连接数据库文件（不存在会自动创建）
    conn = sqlite3.connect(DB_FILE)
 
    # 执行建表语句
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_id      TEXT    NOT NULL,
            url         TEXT,
            price       REAL,
            checked_at  TEXT    NOT NULL
        )
    """)
 
    # 提交更改并关闭连接
    conn.commit()
    conn.close()
    print(f"💾 数据库就绪：{DB_FILE}")
