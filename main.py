import requests
import yaml
import random
from datetime import datetime, timedelta
import json
import os
import sys
import logging

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import time


logging.basicConfig(
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)

def get_seats_with_config(user_config, date_config, seat_config):
    # 二楼东/二楼西/四楼/三楼大厅/守正书院/求新书院/自定义
    seat_name = date_config['name']
    if seat_name == "自定义":
        return user_config['自定义']
    if seat_name not in seat_config:
        raise KeyError(f"座位区域 '{seat_name}' 未在 seat_config.yml 中定义")
    return list(range(seat_config[seat_name]['begin'], seat_config[seat_name]['end']))


class SeatAutoBooker:
    def __init__(self, booker_config):
        self.json = None
        self.resp = None
        self.user_data = None

        logging.info('Creating SeatAutoBooker object')

        self.un = os.environ["SCHOOL_ID"].strip()  # 学号
        print("使用用户：{}".format(self.un))
        self.pd = os.environ["PASSWORD"].strip()  # 密码
        self.SCKey = None
        try:
            self.SCKey = os.environ["SCKEY"]
        except KeyError:
            print("没有Server酱的key,将不会推送消息")

        chrome_options = Options()
        #chrome_options.add_argument('--headless')  # 注释掉可以看到浏览器窗口，便于调试
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        
        # 尝试多种方式加载 chromedriver
        chromedriver_path = None
        
        # 方式1：检查环境变量
        if 'CHROMEDRIVER_PATH' in os.environ:
            chromedriver_path = os.environ['CHROMEDRIVER_PATH']
        
        # 方式2：检查 Windows 缓存目录
        if not chromedriver_path:
            cache_dir = os.path.expanduser('~/.cache/selenium/chromedriver')
            if os.path.exists(cache_dir):
                # 找最新的 chromedriver
                for root, dirs, files in os.walk(cache_dir):
                    if 'chromedriver.exe' in files:
                        chromedriver_path = os.path.join(root, 'chromedriver.exe')
                        break
        
        # 方式3：尝试 Linux 路径（GitHub Actions）
        if not chromedriver_path and os.path.exists('/usr/local/bin/chromedriver'):
            chromedriver_path = '/usr/local/bin/chromedriver'
        
        try:
            if chromedriver_path:
                logging.info(f'尝试使用 ChromeDriver: {chromedriver_path}')
                try:
                    self.driver = webdriver.Chrome(service=Service(chromedriver_path), options=chrome_options)
                except Exception as e:
                    logging.warning(f'指定 ChromeDriver 启动失败 ({e})，回退到 Selenium Manager 自动下载')
                    self.driver = webdriver.Chrome(options=chrome_options)
            else:
                logging.info('使用 Selenium Manager 自动下载 ChromeDriver')
                self.driver = webdriver.Chrome(options=chrome_options)
        except Exception as e:
            logging.error(f'无法加载 ChromeDriver: {e}')
            raise
        self.wait = WebDriverWait(self.driver, 10, 0.5)
        self.cookie = None

        self.cfg = booker_config

    def book_favorite_seat(self, user_config, seat_config, dry_run=False):
        # 判断是否到了预约时间
        # 生活区/自习室 20:30 开始预约，阅览室 21:00 开始预约
        the_day_after_tomorrow = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][(datetime.now().weekday() + 2) % 7]
        date_config = user_config[the_day_after_tomorrow]
        seat_type = seat_config[date_config['name']]["type"]
        tz = self.cfg.get("timezone", 8)
        if seat_type in ("自习室", "生活区"):
            # 20:30 CST 开始预约 = (20:30 - tz) UTC
            start_time = datetime.now().replace(hour=20-tz, minute=30, second=0, microsecond=0)
            end_time = datetime.now().replace(hour=20-tz, minute=45, second=0, microsecond=0)
        else:
            # 阅览室 21:00 CST 开始预约 = (21:00 - tz) UTC
            start_time = datetime.now().replace(hour=21-tz, minute=0, second=0, microsecond=0)
            end_time = datetime.now().replace(hour=21-tz, minute=15, second=0, microsecond=0)
        start_time = start_time - timedelta(minutes=self.cfg["cron-delta-minutes"])
        if not dry_run and (datetime.now() < start_time or datetime.now() > end_time):
            return -1, "未到预约时间"
        logging.info('Booking favorite seat')

        # ========== 第一段预约 ==========
        total_hours = date_config['持续小时数']
        max_cont = date_config.get('单次最大预约', 999)
        needs_split = total_hours > max_cont
        first_duration = max_cont if needs_split else total_hours

        code, message = self._retry_booking(user_config, seat_config, dry_run,
                                            duration_override=first_duration * 3600)
        if code != 0:
            return code, message

        # ========== 第二段预约（如需要拆分） ==========
        if needs_split:
            remaining_hours = total_hours - max_cont
            print(f"\n第一段({max_cont}h)预约成功，继续预约第二段({remaining_hours}h)...")
            code2, msg2 = self._retry_booking(user_config, seat_config, dry_run,
                                              begin_offset_seconds=max_cont * 3600,
                                              duration_override=remaining_hours * 3600)
            if code2 == 0:
                return 0, f"第一段({max_cont}h)成功 + 第二段({remaining_hours}h)成功"
            else:
                return code2, f"第一段({max_cont}h)成功但第二段({remaining_hours}h)失败: {msg2}"

        return code, message

    def _retry_booking(self, user_config, seat_config, dry_run,
                       begin_offset_seconds=0, duration_override=None):
        """带重试的一次预约请求"""
        retry_sleep_time = max(1, timedelta(minutes=self.cfg["cron-delta-minutes"]).seconds*2/(self.cfg["max-retry"]-2) - 10)
        for tried_times in range(1 if dry_run else self.cfg["max-retry"]):
            try:
                code, message = self._book_favorite_seat(user_config, seat_config,
                    tried_times, dry_run, begin_offset_seconds, duration_override)
                if code == 0:
                    return code, message
                print(f"返回码 {code}: {message}，尝试第{tried_times+1}次")
                time.sleep(retry_sleep_time)
            except Exception as e:
                logging.exception(e)
                print(e.__class__, "尝试第{}次".format(tried_times + 1))
                time.sleep(retry_sleep_time)
        return -2, "重试次数用尽，预约失败"

    def _book_favorite_seat(self, user_config, seat_config, tried_times=0, dry_run=False,
                            begin_offset_seconds=0, duration_override=None):
        logging.info('Entering _book_favorite_seat method')
        the_day_after_tomorrow = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][(datetime.now().weekday() + 2) % 7]
        date_config = user_config[the_day_after_tomorrow]
        seats = get_seats_with_config(user_config, date_config, seat_config)
        today_0_clock = datetime.strptime(datetime.now().strftime("%Y-%m-%d 00:00:00"), "%Y-%m-%d %H:%M:%S")
        book_time = today_0_clock + timedelta(days=2) + timedelta(hours=date_config['开始时间']) + timedelta(seconds=begin_offset_seconds)
        delta = book_time - self.cfg["start-time"]
        total_seconds = delta.days * 24 * 3600 + delta.seconds
        if date_config['name'] == '自定义':
            seat = seats[0]  # 自定义固定座位
        else:
            seat = random.choice(seats)
        duration_seconds = duration_override or (3600 * date_config['持续小时数'])
        data = f"beginTime={total_seconds}&duration={duration_seconds}&seats[0]={seat}&seatBookers[0]={self.user_data['uid']}"

        print(f"\n{'='*40} DRY RUN {'='*40}" if dry_run else "")
        offset_hint = f" + {begin_offset_seconds//3600}h偏移" if begin_offset_seconds else ""
        print(f"座位: {seat} | 区域: {date_config['name']}")
        print(f"日期: 后天({the_day_after_tomorrow}) 开始于 {date_config['开始时间']}:00{offset_hint}, 持续 {duration_seconds//3600}h")
        print(f"UID: {self.user_data['uid']} | beginTime(秒): {total_seconds}")
        print(f"POST数据: {data}")
        print(f"请求URL: {self.cfg['target']}")
        print(f"Cookie: {self.cookie[:50]}...")

        if dry_run:
            print(f"\n{'='} 跳过实际POST请求，dry-run模式结束 {'='}")
            return 0, "DRY-RUN 模拟成功"

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        print(f"\n实际发送POST请求...")
        self.resp = requests.post(self.cfg["target"], data=data, headers=headers)
        self.json = json.loads(self.resp.text)
        print(f"API响应: {self.json}")
        return self.json["CODE"], self.json["MESSAGE"] + " 座位:{}".format(seat)

    def login(self):
        logging.info('Login in')

        try:
            logging.info('开始登陆...')

            # 通过 SSO 系统登录
            sso_url = "https://sso.hdu.edu.cn/login?service=https:%2F%2Fhdu.huitu.zhishulib.com%2FUser%2FIndex%2FhduCASLogin%3Fforward%3D%252FSpace%252FCategory%252Fredirect%253Fcategory_id%253D591"
            self.driver.get(sso_url)
            logging.info('打开SSO登录页面: ' + sso_url)

            # 等待用户名输入框出现
            self.wait.until(EC.presence_of_element_located((By.NAME, "username")))
            logging.debug('找到用户名输入框.')

            # 等待密码输入框出现
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
            logging.debug('找到密码输入框.')

            # 等待登录按钮出现
            self.wait.until(EC.presence_of_element_located((By.CLASS_NAME, "login-button")))
            logging.debug('找到登录按钮.')

            # 输入用户名
            username_input = self.driver.find_element(By.NAME, 'username')
            username_input.clear()
            username_input.send_keys(self.un)
            logging.info('输入用户名: {}'.format(self.un))

            # 输入密码
            password_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            password_input.clear()
            password_input.send_keys(self.pd)
            logging.info('输入密码')

            # 点击登录按钮
            logging.info('点击登录按钮')
            login_button = self.driver.find_element(By.CLASS_NAME, "login-button")
            login_button.click()
            
            # 等待登录完成并重定向（等待 URL 离开 SSO 页面）
            try:
                self.wait.until(lambda d: "sso.hdu.edu.cn" not in d.current_url)
                logging.info('登录完成，当前URL: {}'.format(self.driver.current_url))
            except Exception:
                logging.warning('登录后等待重定向超时，当前URL: {}'.format(self.driver.current_url))

            # 获取 Cookie
            cookie_list = self.driver.get_cookies()
            self.cookie = ";".join([item["name"] + "=" + item["value"] + "" for item in cookie_list])
            self.cfg["headers"]['Cookie'] = self.cookie
            logging.info('获取Cookie成功，Cookie数量: {}'.format(len(cookie_list)))

            logging.info("登录成功！")
        except Exception as e:
            logging.error(f"登录失败：{e}")
            return -1
        return 0

    def get_user_info(self):
        logging.info('Getting user info')

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        try:
            resp = requests.get("https://hdu.huitu.zhishulib.com/Seat/Index/searchSeats?LAB_JSON=1",
                                headers=headers)
            self.user_data = resp.json()['DATA']
            _ = self.user_data['uid']
        except Exception as e:
            logging.exception(e)
            print(self.user_data)
            print(e.__class__.__name__ + ",获取用户数据失败")
            return -1
        print("获取用户数据成功")
        return 0

    def wechatNotice(self, message, desp=None):
        logging.info('Sending WeChat notice')

        if self.SCKey:
            url = 'https://sctapi.ftqq.com/{0}.send'.format(self.SCKey)
            data = {
                'title': message,
                'desp': desp or '',
            }
            try:
                r = requests.post(url, data=data)
                if r.json()["data"]["error"] == 'SUCCESS':
                    print("Server酱通知成功")
                else:
                    print("Server酱通知失败")
            except Exception as e:
                logging.exception(e)
                print(e.__class__, "推送服务配置错误")

def is_booking_enable(date_cfg):
    if date_cfg['启用']:
        return True
    return False

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv or "--test" in sys.argv
    if dry_run:
        print("="*50)
        print("DRY RUN MODE - 仅测试登录和请求构造，不实际预约")
        print("="*50)

    logging.info('Start of the program')
    with open("user_config.yml", 'r', encoding='utf-8') as f_obj:
        user_config = yaml.safe_load(f_obj)
    with open("config/basic_config.yml", 'r', encoding='utf-8') as f_obj:
        basic_config = yaml.safe_load(f_obj)
    with open("config/seat_config.yml", 'r', encoding='utf-8') as f_obj:
        seat_config = yaml.safe_load(f_obj)

    the_day_after_tomorrow = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][(datetime.now().weekday() + 2) % 7]
    if not dry_run and not is_booking_enable(user_config[the_day_after_tomorrow]):
        logging.info('预约未启用')
        print("预约未启用")
        exit(0)

    s = SeatAutoBooker(basic_config["SeatAutoBooker"])
    if not s.login() == 0:
        s.driver.quit()
        logging.info('Login unsuccessful')
        exit(-1)
    if not s.get_user_info() == 0:
        s.driver.quit()
        logging.info('Getting user info unsuccessful')
        exit(-1)
    code, message = s.book_favorite_seat(user_config=user_config, seat_config=seat_config, dry_run=dry_run)
    print(f"\n预约结果: [{code}] {message}")
    if not dry_run:
        if code == 0:
            s.wechatNotice("预约成功", message)
        else:
            s.wechatNotice("预约失败", f"错误码{code}: {message}")
    s.driver.quit()
    logging.info('End of the program')
