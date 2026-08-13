# Arduino 固件

打开 `CodexDisplayTest.ino` 前，先将 `secrets.example.h` 复制为 `secrets.h` 并修改配网热点与 OTA 密码。`secrets.h` 已被 Git 忽略。

依赖：Adafruit GFX、Adafruit ST7735 and ST7789、ArduinoJson、WiFiManager。

推荐设置：Generic ESP8266 Module、4 MB Flash、DIO、上传速度 115200。首次烧录需让 GPIO0 接地并复位；之后可访问设备 `/update` 上传导出的 `.bin`。

首次联网时连接 `CodexDisplay-Setup` 热点并打开 `http://192.168.4.1`。加入 2.4 GHz Wi-Fi 后，访问设备 `/control` 切换 Codex、Claude 或自动轮播模式。
