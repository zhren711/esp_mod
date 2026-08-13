# ESP8266 Codex / Claude 状态屏

面向 ESP8266MOD（ESP-12F）和 1.54 英寸 240×240 ST7789 SPI 屏的桌面状态屏。电脑端脚本读取 Codex 与 Claude Code 的本地运行信息及额度，通过局域网把 JSON 状态发送到仅由 Type-C 供电的显示器。

## 当前进度

- ST7789 显示、背光 PWM、局部刷新与 THINKING 文字动画已完成。
- WiFiManager 首次配网；支持 Codex、Claude 和自动轮播模式。
- 提供 `/status/codex`、`/status/claude`、`/health`、`/control` 接口。
- 浏览器访问 `/update` 上传 `.bin`，后续升级无需再次连接 USB-TTL。
- Codex 额度优先读取 app-server，并以 `codex-cli-usage` 的 `usage-limits.json` 为第二数据源。
- Claude 额度读取 Claude Code OAuth 使用量接口。

## 目录

```text
arduino/CodexDisplayTest/   Arduino IDE 固件（当前主版本）
host/codex_lcd.py           Codex + Claude 状态采集与发送
host/test_codex_lcd.py      主机端单元测试
hooks/                      可选状态 Hook 示例
firmware/                   早期 PlatformIO 原型，仅供参考
```

## 硬件引脚

| 信号 | GPIO |
|---|---:|
| TFT SCLK | 14 |
| TFT MOSI | 13 |
| TFT DC | 0 |
| TFT RST | 2 |
| TFT CS | 15 |
| 背光（低电平有效） | 5 |

使用 SPI mode 3，屏幕旋转值为 2。不同 PCB 批次可能采用不同引脚，烧录前请核对。

## 编译与首次烧录

Arduino IDE 需要 ESP8266 Boards 3.1.2+、Adafruit GFX、Adafruit ST7735 and ST7789、ArduinoJson 7 和 WiFiManager。

```powershell
Copy-Item arduino\CodexDisplayTest\secrets.example.h arduino\CodexDisplayTest\secrets.h
```

填写自己的配网和 OTA 口令后，用 Arduino IDE 打开 `arduino/CodexDisplayTest/CodexDisplayTest.ino`。选择 Generic ESP8266 Module、4 MB Flash、DIO、115200 baud。首次烧录需将 GPIO0 接地并复位进入下载模式。

首次启动会创建 `CodexDisplay-Setup` 热点。连接后打开 `http://192.168.4.1`，配置 2.4 GHz Wi-Fi。

## 电脑端

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r host\requirements.txt
Copy-Item host\config.example.json host\config.json
```

修改 `host/config.json` 中的 `device_url`，再运行：

```powershell
.\.venv\Scripts\python host\codex_lcd.py --demo
.\.venv\Scripts\python host\codex_lcd.py --once
.\.venv\Scripts\python host\codex_lcd.py
```

## 后台与 OTA

- `/control`：选择 Codex、Claude 或自动轮播。
- `/update`：使用 `secrets.h` 中的凭据上传 Arduino 导出的 `.bin`。
- `/health`：检查设备在线状态。

## Claude HTTP 401

若出现 `Claude usage warning: HTTP Error 401: Unauthorized`，说明本地 OAuth token 已过期或失效。打开 Claude Code 执行 `/login`，完成浏览器授权后重新运行脚本。token 不会发送给 ESP8266。

## 测试

```powershell
python -m unittest discover -s host -v
```

## 安全说明

- `secrets.h`、`host/config.json`、缓存、字节码和固件二进制均由 `.gitignore` 排除。
- HTTP 接口只适合可信局域网，不应直接暴露到公网。
- 若此前使用过示例口令，请在下一次烧录时更换。

