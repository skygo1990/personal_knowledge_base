import os
import stat

from mcp_server.models import HubError
from mcp_server.storage import checked_path


def trusted_ancestors(path):
    if os.name != "posix":
        raise HubError("UNAUTHORIZED", "当前权限探测仅支持 POSIX；其他系统须单独实现并验收")
    for parent in checked_path(path).parents:
        try:
            info = parent.stat()
        except OSError:
            raise HubError("UNAUTHORIZED", "无法核实受保护路径的父目录权限") from None
        trusted_owner = info.st_uid in {0, os.getuid()}
        sticky = bool(info.st_mode & stat.S_ISVTX)
        if not trusted_owner or (info.st_mode & 0o022 and not sticky):
            raise HubError("UNAUTHORIZED", "受保护路径存在可被不可信身份替换的父目录")


def read_token(path):
    path = checked_path(path)
    trusted_ancestors(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="ascii") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise HubError("UNAUTHORIZED", "访问令牌文件必须仅限服务身份读取")
            token = source.read(4097).strip()
    except (OSError, UnicodeError):
        raise HubError("UNAUTHORIZED", "无法安全读取访问令牌文件") from None
    if not 32 <= len(token) <= 4096 or any(character.isspace() or not character.isprintable() for character in token):
        raise HubError("UNAUTHORIZED", "访问令牌格式无效")
    return token


def validate_runtime(config):
    root = checked_path(config.data_root)
    trusted_ancestors(root)
    if not root.is_dir():
        raise HubError("UNAUTHORIZED", "须由本人预先创建受保护的运行数据目录")
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HubError("UNAUTHORIZED", "私有运行目录必须由当前身份独占读写")
    proposals = checked_path(config.proposal_dir)
    status = checked_path(config.review_status_dir)
    trusted_ancestors(status)
    if not proposals.is_dir() or not status.is_dir():
        raise HubError("UNAUTHORIZED", "须先完成提案及状态投影目录部署")
    if config.security_mode == "single_user":
        trusted_ancestors(proposals)
        for directory in (proposals, status):
            info = directory.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or not os.access(directory, os.W_OK):
                raise HubError("UNAUTHORIZED", "同账户模式的提案与状态目录必须由当前身份独占读写")
        return
    if config.role == "reader":
        if any(os.access(parent, os.W_OK) for parent in (status, *status.parents)):
            raise HubError("UNAUTHORIZED", "读取身份不能修改或替换审核状态投影及其父目录")
        if not os.access(proposals, os.W_OK):
            raise HubError("UNAUTHORIZED", "读取身份缺少本机提案写入权限")
    elif not os.access(status, os.W_OK):
        raise HubError("UNAUTHORIZED", "审核身份不能写状态投影目录")
    elif status.stat().st_uid != os.getuid() or status.stat().st_mode & 0o022:
        raise HubError("UNAUTHORIZED", "状态投影目录不能由其他身份修改")
