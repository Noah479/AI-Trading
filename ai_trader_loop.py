import time
from ai_trader import main_once

while True:
    main_once()
    time.sleep(180)  # 每3分钟执行一次