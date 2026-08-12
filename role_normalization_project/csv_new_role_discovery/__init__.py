"""基于已归一化 CSV 的新岗位发现与岗位定义生成。"""

from .service import DiscoveryConfig, discover_new_roles

__all__ = ["DiscoveryConfig", "discover_new_roles"]
