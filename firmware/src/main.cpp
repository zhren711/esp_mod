#include <Arduino.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>

namespace {
constexpr uint32_t kBaudRate = 115200;
constexpr size_t kMaxLineLength = 768;
constexpr uint32_t kOfflineAfterMs = 10000;

TFT_eSPI display;
String inputLine;
uint32_t lastMessageAt = 0;
bool offlineShown = false;

uint16_t stateColour(const String &state) {
  if (state == "RUNNING") return TFT_ORANGE;
  if (state == "THINKING") return TFT_MAGENTA;
  if (state == "WRITING") return TFT_CYAN;
  if (state == "DONE") return TFT_GREEN;
  if (state == "NEED_CONFIRM") return TFT_WHITE;
  if (state == "WAITING") return TFT_YELLOW;
  if (state == "ERROR") return TFT_RED;
  return TFT_ORANGE;
}

void drawBar(int y, const char *label, int remaining, bool available) {
  display.setTextFont(2);
  display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  display.setCursor(12, y);
  display.print(label);

  constexpr int x = 52;
  constexpr int width = 124;
  constexpr int height = 14;
  display.drawRect(x, y, width, height, TFT_DARKGREY);
  display.fillRect(x + 1, y + 1, width - 2, height - 2, TFT_BLACK);

  display.setCursor(184, y);
  if (!available) {
    display.print("N/A");
    return;
  }

  remaining = constrain(remaining, 0, 100);
  uint16_t colour = remaining > 50 ? TFT_GREEN : (remaining > 20 ? TFT_YELLOW : TFT_RED);
  int fillWidth = (width - 2) * remaining / 100;
  display.fillRect(x + 1, y + 1, fillWidth, height - 2, colour);
  display.printf("%d%%", remaining);
}

void drawWaitingScreen() {
  display.fillScreen(TFT_BLACK);
  display.setTextColor(TFT_CYAN, TFT_BLACK);
  display.setTextFont(4);
  display.setCursor(20, 62);
  display.println("CODEX");
  display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  display.setTextFont(2);
  display.setCursor(28, 112);
  display.println("Waiting for PC...");
  display.setCursor(18, 142);
  display.println("USB serial 115200");
}

void drawOffline() {
  display.fillRect(0, 30, 240, 34, TFT_BLACK);
  display.setTextFont(4);
  display.setTextColor(TFT_RED, TFT_BLACK);
  display.setCursor(12, 34);
  display.print("OFFLINE");
}

void renderStatus(JsonDocument &doc) {
  const String state = doc["state"] | "IDLE";
  const char *model = doc["model"] | "-";
  const char *workspace = doc["workspace"] | "-";
  const char *reset5 = doc["reset5"] | "-";
  const unsigned long tokens = doc["tokens"] | 0UL;
  const bool fiveAvailable = !doc["five_left"].isNull();
  const bool weekAvailable = !doc["week_left"].isNull();
  const int fiveLeft = doc["five_left"] | 0;
  const int weekLeft = doc["week_left"] | 0;

  display.fillScreen(TFT_BLACK);
  display.setTextFont(2);
  display.setTextColor(TFT_CYAN, TFT_BLACK);
  display.setCursor(12, 8);
  display.print("CODEX MONITOR");

  display.setTextFont(4);
  display.setTextColor(stateColour(state), TFT_BLACK);
  display.setCursor(12, 34);
  display.print(state.substring(0, 11));

  display.setTextFont(2);
  display.setTextColor(TFT_WHITE, TFT_BLACK);
  display.setCursor(12, 75);
  display.printf("Model: %.20s", model);
  display.setCursor(12, 96);
  display.printf("Dir:   %.22s", workspace);
  display.setCursor(12, 117);
  display.printf("Tokens: %lu", tokens);

  drawBar(148, "5H", fiveLeft, fiveAvailable);
  drawBar(174, "7D", weekLeft, weekAvailable);

  display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  display.setCursor(12, 207);
  display.printf("5H reset: %.15s", reset5);
}

void consumeLine(const String &line) {
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, line);
  if (error) {
    Serial.printf("ERR invalid json: %s\n", error.c_str());
    return;
  }
  renderStatus(doc);
  lastMessageAt = millis();
  offlineShown = false;
  Serial.println("OK");
}
}  // namespace

void setup() {
  Serial.begin(kBaudRate);
  Serial.setTimeout(50);
  inputLine.reserve(kMaxLineLength);
  display.init();
  display.setRotation(0);
  drawWaitingScreen();
}

void loop() {
  while (Serial.available()) {
    char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      inputLine.trim();
      if (!inputLine.isEmpty()) consumeLine(inputLine);
      inputLine = "";
    } else if (c != '\r') {
      if (inputLine.length() < kMaxLineLength) {
        inputLine += c;
      } else {
        inputLine = "";
        Serial.println("ERR line too long");
      }
    }
  }

  if (lastMessageAt && millis() - lastMessageAt > kOfflineAfterMs && !offlineShown) {
    drawOffline();
    offlineShown = true;
  }
  yield();
}



