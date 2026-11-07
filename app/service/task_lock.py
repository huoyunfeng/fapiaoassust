"""跨操作的重任务锁。

全量匹配重算会批量改写匹配结果，导出 Excel / 备份若与之并发，可能读到
"重算进行到一半"的中间态（SQLite 本身不会报错，但结果不一致）。
这里用一把可重入锁把这类长任务串行化；使用 RLock 是为了允许同线程内嵌套
（例如导出过程中再次获取锁）而不死锁。
"""
import threading

HEAVY_TASK_LOCK = threading.RLock()
