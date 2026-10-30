/*
 * ============================================================================
 *  MCP3301_bench_test.ino
 *  Standalone MCP3301 ADC test - NO sensor involved.
 * ============================================================================
 *
 *  Purpose: verify the MCP3301 SPI read/bit-extraction is correct BEFORE
 *  trusting it in the full C12666MA_spectrometer_MCP3301.ino sketch. This
 *  isolates the ADC as the only variable - no sensor timing, no pixel
 *  clocking, nothing else that could be a confound if something looks wrong.
 *
 *  --- WIRING ------------------------------------------------------------------
 *     Potentiometer outer leg 1 -> Uno 5V
 *     Potentiometer outer leg 2 -> Uno GND
 *     Potentiometer wiper (middle pin) -> MCP3301 IN+
 *     MCP3301 IN-   -> Uno GND
 *     MCP3301 VDD   -> Uno 5V
 *     MCP3301 VSS   -> Uno GND
 *     MCP3301 VREF  -> Uno 5V (same rail as VDD, for this test)
 *     MCP3301 CLK   -> Uno 13 (hardware SCK)
 *     MCP3301 DOUT  -> Uno 12 (hardware MISO)
 *     MCP3301 CS/SHDN -> Uno 9
 *     (Uno pin 11 / MOSI: leave unconnected - MCP3301 has no DIN pin)
 *
 *  --- HOW TO USE -------------------------------------------------------------
 *     1. Wire as above, upload this sketch.
 *     2. Open Serial Monitor at 115200 baud.
 *     3. Slowly turn the pot from one end to the other while watching the
 *        printed raw count and computed voltage. See the checklist below
 *        for what "working correctly" looks like.
 *
 *  --- WHAT TO CHECK ------------------------------------------------------------
 *     [ ] At one extreme of the pot: raw count near 0, voltage near 0.0V
 *     [ ] At the other extreme: raw count near 4095, voltage near 5.0V
 *     [ ] Near the middle: raw count near ~2048, voltage near ~2.5V
 *     [ ] Turning the pot SLOWLY and SMOOTHLY moves the count up/down
 *         smoothly too - no sudden jumps, no resets to 0, no wraparound
 *         to a much smaller number near either extreme.
 *     [ ] The printed "computed" voltage roughly matches what a multimeter
 *         reads directly on the pot's wiper pin (touch the meter probe
 *         right where the wire goes to IN+).
 *
 *     A classic failure sign (seen in several real MCP3301/Arduino reports)
 *     is a value that climbs correctly through most of the range, but
 *     near the very top either plateaus early (e.g. maxing out around
 *     3990 instead of ~4095) or suddenly drops to a smaller number instead
 *     of continuing to climb. If you see that, the bit-extraction in
 *     readMCP3301() likely needs adjustment - worth trying MSBFIRST vs.
 *     re-checking against your MCP3301 datasheet's own timing figure
 *     (DS21700) at that point, since this sketch's bit mapping was
 *     extrapolated rather than verified against a physical device.
 * ============================================================================
 */

#include <SPI.h>

const uint8_t ADC_CS = 9;

void setup() {
  // IMPORTANT: pin 10 is the ATmega328's fixed hardware SS pin. Even
  // though we're not using it (CS is on pin 9 instead), the SPI hardware
  // will silently drop OUT of Master mode if pin 10 is ever left as an
  // INPUT and floats/gets pulled low - which can make every transfer
  // return garbage or all-zero data with no obvious error. Setting it as
  // an OUTPUT (regardless of what it's wired to, even nothing) prevents
  // that entirely.
  pinMode(10, OUTPUT);

  pinMode(ADC_CS, OUTPUT);
  digitalWrite(ADC_CS, HIGH);
  SPI.begin();
  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  Serial.begin(115200);
  Serial.println(F("MCP3301 bench test - turn the pot and watch the values."));
  Serial.println(F("raw_count, voltage"));
}

// Same read function as in the full spectrometer sketch - see its
// PROTOCOL VERIFICATION WARNING comment for details on how this was derived.
int16_t readMCP3301() {
  digitalWrite(ADC_CS, LOW);
  uint8_t hiByte = SPI.transfer(0x00);
  uint8_t loByte = SPI.transfer(0x00);
  digitalWrite(ADC_CS, HIGH);

  // TEST VERSION: discards one additional leading bit versus the original
  // (mask changed 0x3F -> 0x1F, shift changed <<7 -> <<8) to correct an
  // observed ~2x scaling error (max reading was landing at ~half of VREF
  // instead of ~VREF). If this fixes the full-scale reading, the original
  // extraction was off by one bit as suspected - re-verify across several
  // known voltages (0V, ~2.5V, ~5V) before trusting it, since this is
  // still an empirical correction, not a datasheet-confirmed fix.
  int16_t raw = ((int16_t)(hiByte & 0x1F) << 8) | loByte;
  if (raw & 0x1000) raw -= 0x2000;   // sign-extend the 13-bit two's complement value
  return raw;
}

void loop() {
  int16_t raw = readMCP3301();
  float voltage = (raw / 4095.0) * 5.0;   // assumes VREF = 5V, IN- = GND

  Serial.print(raw);
  Serial.print(F(", "));
  Serial.print(voltage, 3);
  Serial.println(F("V"));

  delay(200);   // slow enough to read comfortably, fast enough to feel live
}
