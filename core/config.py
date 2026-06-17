"""
配置加载模块

密钥优先级: 环境变量 > 配置文件 > 默认值
"""

import os
from pathlib import Path
from typing import Any

from logging_config import get_logger

logger = get_logger(__name__)


def _load_dotenv():
    """尝试加载 .env 文件 (python-dotenv 可选依赖)"""
    try:
        from dotenv import load_dotenv
        env_file = os.environ.get("DOTENV_PATH", ".env")
        if os.path.exists(env_file):
            load_dotenv(env_file)
            logger.info("已加载环境变量文件: %s", env_file)
        else:
            logger.debug("未找到 .env 文件 (可忽略)")
    except ImportError:
        logger.debug("python-dotenv 未安装，跳过 .env 加载 (可选依赖)")


def _expand_env(value: Any) -> Any:
    """递归展开字符串中的 ${ENV_VAR} 引用"""
    import re
    if isinstance(value, str):
        def _replace(m):
            var_name = m.group(1)
            return os.environ.get(var_name, m.group(0))
        return re.sub(r'\$\{(\w+)\}', _replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(config_path: str) -> dict:
    """加载 YAML 配置文件，自动展开 ${ENV_VAR} 环境变量引用"""
    import yaml
    _load_dotenv()
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    return _expand_env(config)
