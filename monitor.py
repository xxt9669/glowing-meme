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
    
