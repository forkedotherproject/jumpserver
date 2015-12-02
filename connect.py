# coding: utf-8

import sys

reload(sys)
sys.setdefaultencoding('utf8')

import os
import re
import time
import datetime
import textwrap
import getpass
import readline
import django
import paramiko
import struct, fcntl, signal, socket, select
from io import open as copen
import uuid

os.environ['DJANGO_SETTINGS_MODULE'] = 'jumpserver.settings'
if django.get_version() != '1.6':
    django.setup()
from django.contrib.sessions.models import Session
from jumpserver.api import ServerError, User, Asset, PermRole, AssetGroup, get_object, mkdir, get_asset_info, get_role
from jumpserver.api import logger, Log, TtyLog, get_role_key, CRYPTOR, bash, get_tmp_dir
from jperm.perm_api import gen_resource, get_group_asset_perm, get_group_user_perm, user_have_perm, PermRole
from jumpserver.settings import LOG_DIR
from jperm.ansible_api import Command, MyRunner
from jlog.log_api import escapeString

login_user = get_object(User, username=getpass.getuser())

try:
    import termios
    import tty
except ImportError:
    print '\033[1;31m仅支持类Unix系统 Only unix like supported.\033[0m'
    time.sleep(3)
    sys.exit()


def color_print(msg, color='red', exits=False):
    """
    Print colorful string.
    颜色打印字符或者退出
    """
    color_msg = {'blue': '\033[1;36m%s\033[0m',
                 'green': '\033[1;32m%s\033[0m',
                 'red': '\033[1;31m%s\033[0m'}
    msg = color_msg.get(color, 'blue') % msg
    print msg
    if exits:
        time.sleep(2)
        sys.exit()
    return msg


def write_log(f, msg):
    msg = re.sub(r'[\r\n]', '\r\n', msg)
    f.write(msg)
    f.flush()


class Tty(object):
    """
    A virtual tty class
    一个虚拟终端类，实现连接ssh和记录日志，基类
    """
    def __init__(self, user, asset, role):
        self.username = user.username
        self.asset_name = asset.hostname
        self.ip = None
        self.port = 22
        self.channel = None
        self.asset = asset
        self.user = user
        self.role = role
        self.ssh = None
        self.remote_ip = ''
        self.connect_info = None
        self.login_type = 'ssh'
        self.vim_flag = False
        self.ps1_pattern = re.compile('\[.*@.*\][\$#]')
        self.vim_data = ''

    @staticmethod
    def is_output(strings):
        newline_char = ['\n', '\r', '\r\n']
        for char in newline_char:
            if char in strings:
                return True
        return False

    @staticmethod
    def remove_obstruct_char(cmd_str):
        '''删除一些干扰的特殊符号'''
        control_char = re.compile(r'\x07 | \x1b\[1P | \r ', re.X)
        cmd_str = control_char.sub('',cmd_str.strip())
        patch_char = re.compile('\x08\x1b\[C')      #删除方向左右一起的按键
        while patch_char.search(cmd_str):
            cmd_str = patch_char.sub('', cmd_str.rstrip())
        return cmd_str

    def remove_control_char(self, result_command):    
        """
        处理日志特殊字符
        """
        control_char = re.compile(r"""
                \x1b[ #%()*+\-.\/]. |
                \r |                                               #匹配 回车符(CR)
                (?:\x1b\[|\x9b) [ -?]* [@-~] |                     #匹配 控制顺序描述符(CSI)... Cmd
                (?:\x1b\]|\x9d) .*? (?:\x1b\\|[\a\x9c]) | \x07 |   #匹配 操作系统指令(OSC)...终止符或振铃符(ST|BEL)
                (?:\x1b[P^_]|[\x90\x9e\x9f]) .*? (?:\x1b\\|\x9c) | #匹配 设备控制串或私讯或应用程序命令(DCS|PM|APC)...终止符(ST)
                \x1b.                                              #匹配 转义过后的字符
                [\x80-\x9f] | (?:\x1b\]0.*) | \[.*@.*\][\$#] | (.*mysql>.*)      #匹配 所有控制字符
                """, re.X)
        result_command = control_char.sub('', result_command.strip())
 
        if not self.vim_flag:
            if result_command.startswith('vi') or result_command.startswith('fg'):
                self.vim_flag = True
            return result_command.decode('utf8',"ignore")
        else:
            return ''

    def deal_command(self, str_r):
        """
            处理命令中特殊字符
        """
        str_r = re.sub('\x07', '', str_r)       # 删除响铃
        patch_char = re.compile('\x08\x1b\[C')  # 删除方向左右一起的按键
        while patch_char.search(str_r):
            str_r = patch_char.sub('', str_r.rstrip())

        result_command = ''             # 最后的结果
        backspace_num = 0               # 光标移动的个数
        reach_backspace_flag = False    # 没有检测到光标键则为true
        pattern_str = ''
        while str_r:
            tmp = re.match(r'\s*\w+\s*', str_r)
            if tmp:
                if reach_backspace_flag:
                    pattern_str += str(tmp.group(0))
                    str_r = str_r[len(str(tmp.group(0))):]
                    continue
                else:
                    result_command += str(tmp.group(0))
                    str_r = str_r[len(str(tmp.group(0))):]
                    continue
                
            tmp = re.match(r'\x1b\[K[\x08]*', str_r)
            if tmp:
                if backspace_num > 0:
                    if backspace_num > len(result_command):
                        result_command += pattern_str
                        result_command = result_command[0:-backspace_num]
                    else:
                        result_command = result_command[0:-backspace_num]
                        result_command += pattern_str
                del_len = len(str(tmp.group(0)))-3
                if del_len > 0:
                    result_command = result_command[0:-del_len]
                reach_backspace_flag = False
                backspace_num = 0
                pattern_str = ''
                str_r = str_r[len(str(tmp.group(0))):]
                continue
            
            tmp = re.match(r'\x08+', str_r)
            if tmp:
                str_r = str_r[len(str(tmp.group(0))):]
                if len(str_r) != 0:
                    if reach_backspace_flag:
                        result_command = result_command[0:-backspace_num] + pattern_str
                        pattern_str = ''
                    else:
                        reach_backspace_flag = True
                    backspace_num = len(str(tmp.group(0)))
                    continue
                else:
                    break
            
            if reach_backspace_flag:
                pattern_str += str_r[0]
            else:
                result_command += str_r[0]
            str_r = str_r[1:]
        
        if backspace_num > 0:
            result_command = result_command[0:-backspace_num] + pattern_str

        control_char = re.compile(r"""
                \x1b[ #%()*+\-.\/]. |
                \r |                                               #匹配 回车符(CR)
                (?:\x1b\[|\x9b) [ -?]* [@-~] |                     #匹配 控制顺序描述符(CSI)... Cmd
                (?:\x1b\]|\x9d) .*? (?:\x1b\\|[\a\x9c]) | \x07 |   #匹配 操作系统指令(OSC)...终止符或振铃符(ST|BEL)
                (?:\x1b[P^_]|[\x90\x9e\x9f]) .*? (?:\x1b\\|\x9c) | #匹配 设备控制串或私讯或应用程序命令(DCS|PM|APC)...终止符(ST)
                \x1b.                                              #匹配 转义过后的字符
                [\x80-\x9f] | (?:\x1b\]0.*) | \[.*@.*\][\$#] | (.*mysql>.*)      #匹配 所有控制字符
                """, re.X)
        result_command = control_char.sub('', result_command.strip())
        if not self.vim_flag:
            if result_command.startswith('vi') or result_command.startswith('fg'):
                self.vim_flag = True
            return result_command.decode('utf8', "ignore")
        else:
            return ''

    def get_log(self):
        """
        Logging user command and output.
        记录用户的日志
        """
        tty_log_dir = os.path.join(LOG_DIR, 'tty')
        date_today = datetime.datetime.now()
        date_start = date_today.strftime('%Y%m%d')
        time_start = date_today.strftime('%H%M%S')
        today_connect_log_dir = os.path.join(tty_log_dir, date_start)
        log_file_path = os.path.join(today_connect_log_dir, '%s_%s_%s' % (self.username, self.asset_name, time_start))

        try:
            mkdir(os.path.dirname(today_connect_log_dir), mode=0777)
            mkdir(today_connect_log_dir, mode=0777)
        except OSError:
            logger.debug('创建目录 %s 失败，请修改%s目录权限' % (today_connect_log_dir, tty_log_dir))
            raise ServerError('Create %s failed, Please modify %s permission.' % (today_connect_log_dir, tty_log_dir))

        try:
            # log_file_f = copen(log_file_path + '.log', mode='at', encoding='utf-8', errors='replace')
            # log_time_f = copen(log_file_path + '.time', mode='at', encoding='utf-8', errors='replace')
            log_file_f = open(log_file_path + '.log', 'a')
            log_time_f = open(log_file_path + '.time', 'a')
        except IOError:
            logger.debug('创建tty日志文件失败, 请修改目录%s权限' % today_connect_log_dir)
            raise ServerError('Create logfile failed, Please modify %s permission.' % today_connect_log_dir)

        if self.login_type == 'ssh':  # 如果是ssh连接过来，记录connect.py的pid，web terminal记录为日志的id
            pid = os.getpid()
            self.remote_ip = os.popen("who -m | awk '{ print $5 }'").read().strip('()\n')  # 获取远端IP
        else:
            pid = 0

        log = Log(user=self.username, host=self.asset_name, remote_ip=self.remote_ip, login_type=self.login_type,
                  log_path=log_file_path, start_time=date_today, pid=pid)

        log.save()
        if self.login_type == 'web':
            log.pid = log.id
            log.save()

        log_file_f.write('Start at %s\r\n' % datetime.datetime.now())
        return log_file_f, log_time_f, log

    def get_connect_info(self):
        """
        获取需要登陆的主机的信息和映射用户的账号密码
        """
        asset_info = get_asset_info(self.asset)
        role_key = get_role_key(self.user, self.role)
        role_pass = CRYPTOR.decrypt(self.role.password)
        self.connect_info = {'user': self.user, 'asset': self.asset, 'ip': asset_info.get('ip'),
                             'port': int(asset_info.get('port')), 'role_name': self.role.name,
                             'role_pass': role_pass, 'role_key': role_key}
        logger.debug("Connect: Host: %s Port: %s User: %s Pass: %s Key: %s" % (asset_info.get('ip'),
                                                                               asset_info.get('port'),
                                                                               self.role.name,
                                                                               role_pass,
                                                                               role_key))
        return self.connect_info

    def get_connection(self):
        """
        获取连接成功后的ssh
        """
        connect_info = self.get_connect_info()

        # 发起ssh连接请求 Make a ssh connection
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            role_key = connect_info.get('role_key')
            if role_key and os.path.isfile(role_key):
                try:
                    ssh.connect(connect_info.get('ip'),
                                port=connect_info.get('port'),
                                username=connect_info.get('role_name'),
                                key_filename=role_key,
                                look_for_keys=False)
                    self.ssh = ssh
                    return ssh
                except (paramiko.ssh_exception.AuthenticationException, paramiko.ssh_exception.SSHException):
                    logger.warning('Use ssh key %s Failed.' % role_key)
                    pass

            ssh.connect(connect_info.get('ip'),
                        port=connect_info.get('port'),
                        username=connect_info.get('role_name'),
                        password=connect_info.get('role_pass'),
                        look_for_keys=False)

        except paramiko.ssh_exception.AuthenticationException, paramiko.ssh_exception.SSHException:
            raise ServerError('认证失败 Authentication Error.')
        except socket.error:
            raise ServerError('端口可能不对 Connect SSH Socket Port Error, Please Correct it.')
        else:
            self.ssh = ssh
            return ssh


class SshTty(Tty):
    """
    A virtual tty class
    一个虚拟终端类，实现连接ssh和记录日志
    """

    @staticmethod
    def get_win_size():
        """
        This function use to get the size of the windows!
        获得terminal窗口大小
        """
        if 'TIOCGWINSZ' in dir(termios):
            TIOCGWINSZ = termios.TIOCGWINSZ
        else:
            TIOCGWINSZ = 1074295912L
        s = struct.pack('HHHH', 0, 0, 0, 0)
        x = fcntl.ioctl(sys.stdout.fileno(), TIOCGWINSZ, s)
        return struct.unpack('HHHH', x)[0:2]

    def set_win_size(self, sig, data):
        """
        This function use to set the window size of the terminal!
        设置terminal窗口大小
        """
        try:
            win_size = self.get_win_size()
            self.channel.resize_pty(height=win_size[0], width=win_size[1])
        except Exception:
            pass

    def posix_shell(self):
        """
        Use paramiko channel connect server interactive.
        使用paramiko模块的channel，连接后端，进入交互式
        """
        log_file_f, log_time_f, log = self.get_log()
        old_tty = termios.tcgetattr(sys.stdin)
        pre_timestamp = time.time()
        data = ''
        input_mode = False
        try:
            tty.setraw(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            self.channel.settimeout(0.0)

            while True:
                try:
                    r, w, e = select.select([self.channel, sys.stdin], [], [])
                except Exception:
                    pass

                if self.channel in r:
                    try:
                        x = self.channel.recv(1024)
                        if len(x) == 0:
                            break
                        if self.vim_flag:
                            self.vim_data += x
                        sys.stdout.write(x)
                        sys.stdout.flush()
                        now_timestamp = time.time()
                        log_time_f.write('%s %s\n' % (round(now_timestamp-pre_timestamp, 4), len(x)))
                        log_time_f.flush()
                        log_file_f.write(x)
                        log_file_f.flush()
                        pre_timestamp = now_timestamp
                        log_file_f.flush()

                        if input_mode and not self.is_output(x):
                            data += x

                    except socket.timeout:
                        pass

                if sys.stdin in r:
                    x = os.read(sys.stdin.fileno(), 1)
                    input_mode = True
                    if str(x) in ['\r', '\n', '\r\n']:
                        if self.vim_flag:
                            match = self.ps1_pattern.search(self.vim_data)
                            if match:
                                self.vim_flag = False
                                data = self.deal_command(data)[0:200]
                                if len(data) > 0:
                                    TtyLog(log=log, datetime=datetime.datetime.now(), cmd=data).save()
                        else:
                            data = self.deal_command(data)[0:200]
                            if len(data) > 0:
                                TtyLog(log=log, datetime=datetime.datetime.now(), cmd=data).save()
                        data = ''
                        self.vim_data = ''
                        input_mode = False

                    if len(x) == 0:
                        break
                    self.channel.send(x)

        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
            log_file_f.write('End time is %s' % datetime.datetime.now())
            log_file_f.close()
            log_time_f.close()
            log.is_finished = True
            log.end_time = datetime.datetime.now()
            log.save()

    def connect(self):
        """
        Connect server.
        连接服务器
        """
        ps1 = "PS1='[\u@%s \W]\$ '\n" % self.ip
        login_msg = "clear;echo -e '\\033[32mLogin %s done. Enjoy it.\\033[0m'\n" % self.ip

        # 发起ssh连接请求 Make a ssh connection
        ssh = self.get_connection()

        # 获取连接的隧道并设置窗口大小 Make a channel and set windows size
        global channel
        win_size = self.get_win_size()
        self.channel = channel = ssh.invoke_shell(height=win_size[0], width=win_size[1], term='xterm')
        try:
            signal.signal(signal.SIGWINCH, self.set_win_size)
        except:
            pass

        # 设置PS1并提示 Set PS1 and msg it
        #channel.send(ps1)
        #channel.send(login_msg)
        # channel.send('echo ${SSH_TTY}\n')
        # global SSH_TTY
        # while not channel.recv_ready():
        #     time.sleep(1)
        # tmp = channel.recv(1024)
        #print 'ok'+tmp+'ok'
        # SSH_TTY  = re.search(r'(?<=/dev/).*', tmp).group().strip()
        # SSH_TTY = ''
        # channel.send('clear\n')
        # Make ssh interactive tunnel
        self.posix_shell()

        # Shutdown channel socket
        channel.close()
        ssh.close()


class Nav(object):
    def __init__(self, user):
        self.user = user
        self.search_result = {}
        self.user_perm = {}

    @staticmethod
    def print_nav():
        """
        Print prompt
        打印提示导航
        """
        msg = """\n\033[1;32m###  Welcome To Use JumpServer, A Open Source System . ### \033[0m
        1) Type \033[32mID\033[0m To Login.
        2) Type \033[32m/\033[0m + \033[32mIP, Host Name, Host Alias or Comments \033[0mTo Search.
        3) Type \033[32mP/p\033[0m To Print The Servers You Available.
        4) Type \033[32mG/g\033[0m To Print The Server Groups You Available.
        5) Type \033[32mG/g\033[0m\033[0m + \033[32mGroup ID\033[0m To Print The Server Group You Available.
        6) Type \033[32mE/e\033[0m To Execute Command On Several Servers.
        7) Type \033[32mQ/q\033[0m To Quit.
        """

        msg = """\n\033[1;32m###  欢迎使用Jumpserver开源跳板机  ### \033[0m
        1) 输入 \033[32mID\033[0m 直接登录.
        2) 输入 \033[32m/\033[0m + \033[32mIP, 主机名, 主机别名 or 备注 \033[0m搜索.
        3) 输入 \033[32mP/p\033[0m 显示您有权限的主机.
        4) 输入 \033[32mG/g\033[0m 显示您有权限的主机组.
        5) 输入 \033[32mG/g\033[0m\033[0m + \033[32m组ID\033[0m 显示该组下主机.
        6) 输入 \033[32mE/e\033[0m 批量执行命令.
        7) 输入 \033[32mU/u\033[0m 批量上传文件.
        7) 输入 \033[32mD/d\033[0m 批量下载文件.
        7) 输入 \033[32mQ/q\033[0m 退出.
        """
        print textwrap.dedent(msg)

    def search(self, str_r=''):
        gid_pattern = re.compile(r'^g\d+$')
        # 获取用户授权的所有主机信息
        if not self.user_perm:
            self.user_perm = get_group_user_perm(self.user)
        user_asset_all = self.user_perm.get('asset').keys()
        # 搜索结果保存
        user_asset_search = []
        if str_r:
            # 资产组组id匹配
            if gid_pattern.match(str_r):
                gid = int(str_r.lstrip('g'))
                # 获取资产组包含的资产
                user_asset_search = get_object(AssetGroup, id=gid).asset_set.all()
            else:
                # 匹配 ip, hostname, 备注
                for asset in user_asset_all:
                    if str_r in asset.ip or str_r in str(asset.hostname) or str_r in str(asset.comment):
                        user_asset_search.append(asset)
        else:
            # 如果没有输入就展现所有
            user_asset_search = user_asset_all

        self.search_result = dict(zip(range(len(user_asset_search)), user_asset_search))
        print '\033[32m[%-3s] %-15s  %-15s  %-5s  %-10s  %s \033[0m' % ('ID', 'AssetName', 'IP', 'Port', 'Role', 'Comment')
        for index, asset in self.search_result.items():
            # 获取该资产信息
            asset_info = get_asset_info(asset)
            # 获取该资产包含的角色
            role = [str(role.name) for role in self.user_perm.get('asset').get(asset).get('role')]
            if asset.comment:
                print '[%-3s] %-15s  %-15s  %-5s  %-10s  %s' % (index, asset.hostname, asset.ip, asset_info.get('port'),
                                                                role, asset.comment)
            else:
                print '[%-3s] %-15s  %-15s  %-5s  %-10s' % (index, asset.hostname, asset.ip, asset_info.get('port'), role)
        print

    def print_asset_group(self):
        """
        打印用户授权的资产组
        """
        user_asset_group_all = get_group_user_perm(self.user).get('asset_group', [])

        print '\033[32m[%-3s] %-15s %s \033[0m' % ('ID', 'GroupName', 'Comment')
        for asset_group in user_asset_group_all:
            if asset_group.comment:
                print '[%-3s] %-15s %s' % (asset_group.id, asset_group.name, asset_group.comment)
            else:
                print '[%-3s] %-15s' % (asset_group.id, asset_group.name)
        print

    def get_exec_log(self, assets_name_str):
        exec_log_dir = os.path.join(LOG_DIR, 'exec')
        date_today = datetime.datetime.now()
        date_start = date_today.strftime('%Y%m%d')
        time_start = date_today.strftime('%H%M%S')
        today_connect_log_dir = os.path.join(exec_log_dir, date_start)
        log_file_path = os.path.join(today_connect_log_dir, '%s_%s' % (self.user.username, time_start))

        try:
            mkdir(os.path.dirname(today_connect_log_dir), mode=0777)
            mkdir(today_connect_log_dir, mode=0777)
        except OSError:
            logger.debug('创建目录 %s 失败，请修改%s目录权限' % (today_connect_log_dir, exec_log_dir))
            raise ServerError('Create %s failed, Please modify %s permission.' % (today_connect_log_dir, exec_log_dir))

        try:
            log_file_f = open(log_file_path + '.log', 'a')
            log_file_f.write('Start at %s\r\n' % datetime.datetime.now())
            log_time_f = open(log_file_path + '.time', 'a')
        except IOError:
            logger.debug('创建tty日志文件失败, 请修改目录%s权限' % today_connect_log_dir)
            raise ServerError('Create logfile failed, Please modify %s permission.' % today_connect_log_dir)

        remote_ip = os.popen("who -m | awk '{ print $5 }'").read().strip('()\n')
        log = Log(user=self.user.username, host=assets_name_str, remote_ip=remote_ip, login_type='exec',
                  log_path=log_file_path, start_time=datetime.datetime.now(), pid=os.getpid())
        log.save()
        return log_file_f, log_time_f, log

    def exec_cmd(self):
        """
        批量执行命令
        """
        while True:
            if not self.user_perm:
                self.user_perm = get_group_user_perm(self.user)
            print '\033[32m[%-2s] %-15s \033[0m' % ('ID', '角色')
            roles = self.user_perm.get('role').keys()
            role_check = dict(zip(range(len(roles)), roles))

            for i, r in role_check.items():
                print '[%-2s] %-15s' % (i, r.name)
            print
            print "请输入运行命令角色的ID, q退出"

            try:
                role_id = raw_input("\033[1;32mRole>:\033[0m ").strip()
                if role_id == 'q':
                    break
                else:
                    role = role_check[int(role_id)]
                    assets = list(self.user_perm.get('role', {}).get(role).get('asset'))
                    print "该角色有权限的所有主机"
                    for asset in assets:
                        print asset.hostname
                    print
                    print "请输入主机名、IP或ansile支持的pattern, q退出"
                    pattern = raw_input("\033[1;32mPattern>:\033[0m ").strip()
                    if pattern == 'q':
                        break
                    else:
                        res = gen_resource({'user': self.user, 'asset': assets, 'role': role}, perm=self.user_perm)
                        cmd = Command(res)
                        logger.debug("批量执行res: %s" % res)
                        asset_name_str = ''
                        for inv in cmd.inventory.get_hosts(pattern=pattern):
                            print inv.name
                            asset_name_str += inv.name
                        print

                        log_file_f, log_time_f, log = self.get_exec_log(asset_name_str)
                        pre_timestamp = time.time()
                        while True:
                            print "请输入执行的命令， 按q退出"
                            data = 'ansible> '
                            write_log(log_file_f, data)
                            now_timestamp = time.time()
                            write_log(log_time_f, '%s %s\n' % (round(now_timestamp-pre_timestamp, 4), len(data)))
                            pre_timestamp = now_timestamp
                            command = raw_input("\033[1;32mCmds>:\033[0m ").strip()
                            data = '%s\r\n' % command
                            write_log(log_file_f, data)
                            now_timestamp = time.time()
                            write_log(log_time_f, '%s %s\n' % (round(now_timestamp-pre_timestamp, 4), len(data)))
                            pre_timestamp = now_timestamp
                            TtyLog(log=log, cmd=command, datetime=datetime.datetime.now()).save()
                            if command == 'q':
                                log.is_finished = True
                                log.end_time = datetime.datetime.now()
                                log.save()
                                break
                            result = cmd.run(module_name='shell', command=command, pattern=pattern)
                            for k, v in result.items():
                                if k == 'ok':
                                    for host, output in v.items():
                                        header = color_print("%s => %s" % (host, 'Ok'), 'green')
                                        print output
                                        output = re.sub(r'[\r\n]', '\r\n', output)
                                        data = '%s\r\n%s\r\n' % (header, output)
                                        now_timestamp = time.time()
                                        write_log(log_file_f, data)
                                        write_log(log_time_f, '%s %s\n' % (round(now_timestamp-pre_timestamp, 4), len(data)))
                                        pre_timestamp = now_timestamp
                                        print
                                else:
                                    for host, output in v.items():
                                        header = color_print("%s => %s" % (host, k), 'red')
                                        output = color_print(output, 'red')
                                        output = re.sub(r'[\r\n]', '\r\n', output)
                                        data = '%s\r\n%s\r\n' % (header, output)
                                        now_timestamp = time.time()
                                        write_log(log_file_f, data)
                                        write_log(log_time_f, '%s %s\n' % (round(now_timestamp-pre_timestamp, 4), len(data)))
                                        pre_timestamp = now_timestamp
                                        print
                                print "=" * 20
                                print

            except (IndexError, KeyError):
                color_print('ID输入错误')
                continue

            except EOFError:
                print
                break
            finally:
                log.is_finished = True
                log.end_time = datetime.datetime.now()

    def upload(self):
        while True:
            if not self.user_perm:
                self.user_perm = get_group_user_perm(self.user)
            try:
                print "请输入主机名、IP或ansile支持的pattern, q退出"
                pattern = raw_input("\033[1;32mPattern>:\033[0m ").strip()
                if pattern == 'q':
                    break
                else:
                    assets = self.user_perm.get('asset').keys()
                    res = gen_resource({'user': self.user, 'asset': assets}, perm=self.user_perm)
                    runner = MyRunner(res)
                    logger.debug("Muti upload file res: %s" % res)
                    asset_name_str = ''
                    for inv in runner.inventory.get_hosts(pattern=pattern):
                        print inv.name
                        asset_name_str += inv.name
                    print
                    tmp_dir = get_tmp_dir()
                    logger.debug('Upload tmp dir: %s' % tmp_dir)
                    os.chdir(tmp_dir)
                    bash('rz')
                    check_notempty = os.listdir(tmp_dir)
                    if not check_notempty:
                        print color_print("上传文件为空")
                        continue
                    runner = MyRunner(res)
                    runner.run('copy', module_args='src=%s dest=%s directory_mode'
                                                     % (tmp_dir, tmp_dir), pattern=pattern)
                    ret = runner.get_result()
                    logger.debug(ret)
                    if ret.get('failed'):
                        print ret
                        error = '上传目录: %s \n上传失败: [ %s ] \n上传成功 [ %s ]' % (tmp_dir,
                                                                             ', '.join(ret.get('failed').keys()),
                                                                             ', '.join(ret.get('ok')))
                        color_print(error)
                    else:
                        msg = '上传目录: %s \n传送成功 [ %s ]' % (tmp_dir, ', '.join(ret.get('ok')))
                        color_print(msg, 'green')
                    print

            except IndexError:
                pass

    def download(self):
        while True:
            if not self.user_perm:
                self.user_perm = get_group_user_perm(self.user)
            try:
                print "进入批量下载模式"
                print "请输入主机名、IP或ansile支持的pattern, q退出"
                pattern = raw_input("\033[1;32mPattern>:\033[0m ").strip()
                if pattern == 'q':
                    break
                else:
                    assets = self.user_perm.get('asset').keys()
                    res = gen_resource({'user': self.user, 'asset': assets}, perm=self.user_perm)
                    runner = MyRunner(res)
                    logger.debug("Muti Muti file res: %s" % res)
                    for inv in runner.inventory.get_hosts(pattern=pattern):
                        print inv.name
                    print
                    tmp_dir = get_tmp_dir()
                    logger.debug('Download tmp dir: %s' % tmp_dir)
                    while True:
                        print "请输入文件路径(不支持目录)"
                        file_path = raw_input("\033[1;32mPath>:\033[0m ").strip()
                        if file_path == 'q':
                            break
                        runner.run('fetch', module_args='src=%s dest=%s' % (file_path, tmp_dir), pattern=pattern)
                        ret = runner.get_result()
                        os.chdir('/tmp')
                        tmp_dir_name = os.path.basename(tmp_dir)
                        bash('tar czf %s.tar.gz %s ' % (tmp_dir, tmp_dir_name))

                        if ret.get('failed'):
                            print ret
                            error = '文件名称: %s 下载失败: [ %s ] \n下载成功 [ %s ]' % \
                                    ('%s.tar.gz' % tmp_dir_name, ', '.join(ret.get('failed').keys()), ', '.join(ret.get('ok')))
                            color_print(error)
                        else:
                            msg = '文件名称: %s 下载成功 [ %s ]' % ('%s.tar.gz' % tmp_dir_name, ', '.join(ret.get('ok')))
                            color_print(msg, 'green')
                        print
            except IndexError:
                pass


def main():
    """
    he he
    主程序
    """
    if not login_user:  # 判断用户是否存在
        color_print(u'没有该用户，或许你是以root运行的 No that user.', exits=True)

    gid_pattern = re.compile(r'^g\d+$')
    nav = Nav(login_user)
    nav.print_nav()

    try:
        while True:
            try:
                option = raw_input("\033[1;32mOpt or ID>:\033[0m ").strip()
            except EOFError:
                nav.print_nav()
                continue
            except KeyboardInterrupt:
                sys.exit(0)
            if option in ['P', 'p', '\n', '']:
                nav.search()
                continue
            if option.startswith('/') or gid_pattern.match(option):
                nav.search(option.lstrip('/'))
            elif option in ['G', 'g']:
                nav.print_asset_group()
                continue
            elif option in ['E', 'e']:
                nav.exec_cmd()
                continue
            elif option in ['U', 'u']:
                nav.upload()
            elif option in ['D', 'd']:
                nav.download()
            elif option in ['Q', 'q', 'exit']:
                sys.exit()
            else:
                try:
                    asset = nav.search_result[int(option)]
                    roles = get_role(login_user, asset)
                    if len(roles) > 1:
                        role_check = dict(zip(range(len(roles)), roles))
                        print "\033[32m[ID] 角色\033[0m"
                        for index, role in role_check.items():
                            print "[%-2s] %s" % (index, role.name)
                        print
                        print "授权角色超过1个，请输入角色ID, q退出"
                        try:
                            role_index = raw_input("\033[1;32mID>:\033[0m ").strip()
                            if role_index == 'q':
                                continue
                            else:
                                role = role_check[int(role_index)]
                        except IndexError:
                            color_print('请输入正确ID', 'red')
                            continue
                    elif len(roles) == 1:
                        role = roles[0]
                    else:
                        color_print('没有映射用户', 'red')
                        continue
                    ssh_tty = SshTty(login_user, asset, role)
                    ssh_tty.connect()
                except (KeyError, ValueError):
                    color_print('请输入正确ID', 'red')
                except ServerError, e:
                    color_print(e, 'red')
    except IndexError:
        pass

if __name__ == '__main__':
    main()
