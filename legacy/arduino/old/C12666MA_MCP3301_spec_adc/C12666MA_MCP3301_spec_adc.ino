/*
 * ============================================================================
 *  C12666MA_spectrometer_MCP3301.ino
 *  Hamamatsu C12666MA micro-spectrometer reader, sampled via an external
 *  MCP3301 13-bit SPI ADC instead of the Arduino Uno's built-in 10-bit ADC.
 * ============================================================================
 *
 *  This is a variant of C12666MA_spectrometer.ino - same sensor timing and
 *  serial command protocol, but VIDEO is now digitized by an MCP3301 over
 *  SPI for more resolution (up to 4096 levels used here vs. 1024 before -
 *  see the RESOLUTION NOTE below for why it's 4096, not the full 8192 the
 *  chip is capable of).
 *
 *  --- WIRING: SENSOR SIDE (unchanged from the analogRead version) -----------
 *     Sensor CLK   -> Uno A2   (digital output)
 *     Sensor ST    -> Uno A1   (digital output)
 *     Sensor VIDEO -> op-amp buffer -> MCP3301 IN+  (see ADC WIRING below)
 *     Sensor Gain  -> Uno A0   (digital output)
 *     Sensor +Vs   -> Uno 5V
 *     Sensor GND   -> Uno GND
 *
 *  --- WIRING: MCP3301 ADC SIDE -------------------------------------------------
 *     MCP3301 VDD   -> Uno 5V
 *     MCP3301 VSS   -> Uno GND
 *     MCP3301 VREF  -> a stable reference voltage. Tying this to the same
 *                      5V rail as VDD is the simplest option and is what
 *                      this sketch assumes, but your reading accuracy is
 *                      then only as good as that rail's stability - any
 *                      noise/ripple on Vdd becomes noise in every count.
 *                      A dedicated precision reference IC (e.g. a 4.096V
 *                      reference) will give more consistent results.
 *     MCP3301 IN+   -> your buffered VIDEO signal (op-amp output)
 *     MCP3301 IN-   -> Uno GND (see RESOLUTION NOTE below)
 *     MCP3301 CLK   -> Uno 13 (hardware SCK) - NOT the same signal as the
 *                      sensor's own CLK pin above! Same name, two totally
 *                      different clocks. Easy mistake to make - double
 *                      check you haven't wired both "CLK"s to the same pin.
 *     MCP3301 DOUT  -> Uno 12 (hardware MISO)
 *     MCP3301 CS/SHDN -> Uno 9 (ADC_CS below; any free digital pin works,
 *                      change ADC_CS if you use a different one)
 *     (MCP3301 has no DIN pin - it can't be written to, only read from, so
 *     Uno pin 11 / MOSI is not needed for this chip specifically, though
 *     the hardware SPI peripheral will still toggle it harmlessly.)
 *
 *  --- RESOLUTION NOTE -----------------------------------------------------------
 *     The MCP3301 is a full DIFFERENTIAL 13-bit ADC: its output is a signed
 *     value from -4096 to +4095, representing (IN+ - IN-) relative to
 *     +/-VREF. Tying IN- straight to GND (the simple option used here)
 *     means your always-positive VIDEO signal only ever uses the positive
 *     half of that range - effectively 12 usable bits (0-4095), not the
 *     full 13. That's still a 4x improvement over the Uno's native 10-bit
 *     ADC (1024 levels), just not the chip's full spec'd capability. Using
 *     the full 13 bits would require biasing IN- to a fixed offset instead
 *     of GND - a bigger hardware change not implemented here.
 *
 *  --- PROTOCOL VERIFICATION STATUS -----------------------------------------------
 *     The SPI bit-extraction in readMCP3301() below was empirically verified
 *     using MCP3301_bench_test.ino: fed a known DC voltage from a
 *     potentiometer (0V, ~2.5V, ~5V, and a full smooth sweep in between)
 *     and confirmed the reported value tracks linearly and correctly
 *     across the whole range. An earlier version consistently read almost
 *     exactly half the true voltage at full-scale (a dropped top bit) -
 *     this version corrects that. If you change the wiring, breakout
 *     board, or chip, it's worth re-running that bench test again before
 *     trusting real data from it - see MCP3301_bench_test.ino for the
 *     procedure and what to look for.
 *
 *  --- ACQUISITION PROTOCOL (verified against groupgets/c12666ma) -------------
 *     The C12666MA's readout sequence is NOT the same shape as the C12880MA's
 *     and differs in two important ways:
 *       1. VIDEO only updates once every 4 CLK pulses (the datasheet states
 *          "video output is 1/4 of the clock pulse frequency"), so each
 *          pixel needs a 4-clock group, with the valid sample taken on the
 *          first clock's falling edge.
 *       2. ST idles HIGH and is only pulsed briefly LOW - once to mark the
 *          start of integration, once to mark the end - rather than staying
 *          low for the whole pixel readout.
 *     This sketch's acquireOnePass() follows that six-step sequence (leading
 *     clocks, start pulse, integration, stop pulse, 4-clocks-per-pixel read,
 *     trailing clocks), matching the reference sketch at:
 *       https://github.com/groupgets/c12666ma
 *       https://github.com/tricorderproject/arducordermini
 *
 *  --- HOW TO USE -------------------------------------------------------------
 *     1. Upload with the Arduino IDE (board: "Arduino Uno").
 *     2. Open Serial Monitor at 115200 baud, line ending = "Carriage Return".
 *     3. Type commands (see the table in handleCommand() below), e.g.:
 *          hello
 *          set_integration,2000
 *          set_gain,1      (0 = low gain, 1 = high gain)
 *          dark            (capture a dark reference with the sensor covered)
 *          spec            (dark-subtracted spectrum, 256 numbers)
 *          spec,raw        (raw spectrum, 256 numbers, now 0-4095 range)
 * ============================================================================
 */

#include <EEPROM.h>
#include <SPI.h>

// ============================================================================
//  1. PINS & CONSTANTS
// ============================================================================

// Pin assignments (match the GroupGets breakout layout).
const uint8_t SPEC_CLK   = A2;   // clock output to the sensor
const uint8_t SPEC_ST    = A1;   // start pulse output to the sensor
const uint8_t SPEC_GAIN  = A0;   // gain select output to the sensor (pin 9)
// Note: there is no SPEC_VIDEO analog pin anymore - VIDEO is now read via
// the MCP3301 over SPI (see readMCP3301() below), not analogRead().

// MCP3301 chip-select pin. CLK/DOUT/MOSI use the Uno's fixed hardware SPI
// pins (13/12/11) and don't need separate constants.
const uint8_t ADC_CS = 9;

// Gain pin logic levels (see GAIN PIN note above).
const uint8_t GAIN_LOW  = HIGH;  // low gain:  pin at/near Vdd
const uint8_t GAIN_HIGH = LOW;   // high gain: pin at/near GND

// The sensor has 256 pixels (spectral channels).
const int SPEC_CHANNELS = 256;

// Half-period of each clock pulse, in microseconds. 1 us gives a clean,
// slow clock that the sensor is happy with. Bigger = slower and safer.
// The C12666MA datasheet allows a clock frequency of 1 kHz to 800 kHz
// (f(CLK)), so this slow default clock is comfortably within range.
const int CLK_DELAY_US = 1;

// Datasheet-specified minimum integration time for the C12666MA is about
// 1.28 ms. We enforce this floor in set_integration() so a mistaken
// set_integration,0 command can't silently produce underexposed frames.
const unsigned long MIN_INTEGRATION_US = 1280UL;

// Averaging cap. With the MCP3301's ~12 usable bits (max ~4095 per pixel,
// see RESOLUTION NOTE above), g_data's accumulator must be wide enough to
// hold MAX_AVG * 4095 without overflow - see the uint32_t g_data type below
// (this was uint16_t in the analogRead version, which would have overflowed
// here: 63 * 4095 = 258185, way past 65535). uint32_t comfortably covers
// this at any MAX_AVG up to 255, so MAX_AVG itself no longer needs to be
// small for overflow reasons - kept at 63 mainly to bound acquisition time.
const uint8_t MAX_AVG = 15;

// ============================================================================
//  2. CONFIGURATION STORED IN EEPROM
// ============================================================================
// The Uno has no filesystem, so settings that must survive a power cycle are
// kept in EEPROM. A "magic" byte lets us detect a blank chip and load defaults.

const uint8_t CONFIG_MAGIC = 0x66;   // bumped from the C12880MA version (0x43)
                                      // since the struct below gained a field -
                                      // change this again if you change the struct

struct Config {
  uint8_t       magic;             // must equal CONFIG_MAGIC if valid
  char          name[16];          // human-friendly device name
  unsigned long integration_us;    // extra exposure time (microseconds)
  uint8_t       n_avg;             // number of frames to average (1..MAX_AVG)
  float         wl_coeffs[6];      // wavelength polynomial A0..A5 (used host-side)
  uint8_t       high_gain;         // 0 = low gain, 1 = high gain
};

Config g_cfg;   // the live copy of our settings

// ============================================================================
//  3. FRAME BUFFERS  (the biggest users of RAM)
// ============================================================================

uint16_t g_data[SPEC_CHANNELS];   // latest frame; also used as the average sum
                                   // (widened from uint16_t: with the MCP3301's
                                   // ~12-bit range, MAX_AVG * ~4095 can exceed
                                   // 65535, see MAX_AVG note above)
uint16_t g_dark[SPEC_CHANNELS];   // dark reference frame (post-averaging, so a
                                   // single value never exceeds ~4095 - uint16_t
                                   // is still fine here)
bool     g_have_dark = false;     // true once a dark frame has been captured

// ============================================================================
//  4. SERIAL COMMAND BUFFER
// ============================================================================
// We read characters into a fixed char array (not a String) to avoid the
// memory-fragmentation problems that String can cause on small AVR chips.

char    g_cmd[40];
uint8_t g_cmd_len = 0;

// ---------------------------------------------------------------------------
//  Soft reset: jumping to address 0 restarts the sketch. (Simple, not a full
//  hardware reset, but fine for the "reboot" command.)
// ---------------------------------------------------------------------------
void (*resetBoard)(void) = 0;

// ============================================================================
//  EEPROM helpers
// ============================================================================

void saveConfig() {
  EEPROM.put(0, g_cfg);
}

void loadConfig() {
  EEPROM.get(0, g_cfg);
  if (g_cfg.magic != CONFIG_MAGIC) {
    // Blank or outdated EEPROM: fill in sensible defaults and store them.
    g_cfg.magic = CONFIG_MAGIC;
    strcpy(g_cfg.name, "C12666MA");
    g_cfg.integration_us = MIN_INTEGRATION_US;   // datasheet minimum, not 0
    g_cfg.n_avg = 1;               // no averaging by default
    for (int i = 0; i < 6; i++) g_cfg.wl_coeffs[i] = 0.0;
    g_cfg.high_gain = 0;           // low gain by default
    saveConfig();
  }
  // Make sure a corrupted value can never break averaging or integration.
  if (g_cfg.n_avg < 1)       g_cfg.n_avg = 1;
  if (g_cfg.n_avg > MAX_AVG) g_cfg.n_avg = MAX_AVG;
  if (g_cfg.integration_us < MIN_INTEGRATION_US) g_cfg.integration_us = MIN_INTEGRATION_US;
}

// ============================================================================
//  5. ACQUISITION
// ============================================================================

// One low/high clock pulse on SPEC_CLK (order matches the reference sketch;
// what matters for a free-running train is that transitions are evenly
// spaced, so this is equivalent to a high/low pulse for anything except the
// edge-critical start/stop pulses handled separately in pulseStartStop()).
void pulseClock() {
  digitalWrite(SPEC_CLK, LOW);
  delayMicroseconds(CLK_DELAY_US);
  digitalWrite(SPEC_CLK, HIGH);
  delayMicroseconds(CLK_DELAY_US);
}

// Push the current gain setting out to the sensor's Gain pin.
void applyGain() {
  digitalWrite(SPEC_GAIN, g_cfg.high_gain ? GAIN_HIGH : GAIN_LOW);
}

// Trigger one conversion on the MCP3301 over SPI and return its signed
// 13-bit result (-4096..+4095).
//
// Two bytes (16 SPI clocks) are transferred; the ADC's useful data spans
// bits within those 16 clocks as follows (MSB-first):
//   byte1 bits [7:5] = don't-care/leading bits (discarded)
//   byte1 bits [4:0] = data bits [12:8]  (5 bits, includes the sign bit)
//   byte2 bits [7:0] = data bits [7:0]   (8 bits)
// giving 5 + 8 = 13 total data bits, sign-extended below since the result
// is two's complement.
//
// This extraction was empirically verified on real hardware using
// MCP3301_bench_test.ino: fed a known DC voltage from a potentiometer
// (0V, ~2.5V, ~5V, and a full smooth sweep in between) and confirmed the
// output tracks linearly and correctly across the whole range. An earlier
// version of this function (one fewer discarded leading bit) consistently
// read almost exactly half of the true voltage at full-scale - a classic
// signature of a dropped top bit - which is what led to this fix. If you
// substitute a different MCP3301 breakout/wiring, it's still worth
// re-running that same bench test before trusting real data from it.
int16_t readMCP3301() {
  digitalWrite(ADC_CS, LOW);
  uint8_t hiByte = SPI.transfer(0x00);
  uint8_t loByte = SPI.transfer(0x00);
  digitalWrite(ADC_CS, HIGH);

  int16_t raw = ((int16_t)(hiByte & 0x1F) << 8) | loByte;
  if (raw & 0x1000) raw -= 0x2000;   // sign-extend the 13-bit two's complement value
  return raw;
}

// Same as readMCP3301(), but clamped to 0 for our use case: with IN- tied
// to GND (see RESOLUTION NOTE at top), VIDEO should never legitimately
// produce a negative reading - any negative result is noise near the
// zero point, not a real signal, so we floor it at 0 rather than let it
// wrap oddly when accumulated into the unsigned g_data[] buffer.
uint16_t readMCP3301Unsigned() {
  int16_t raw = readMCP3301();
  return (raw < 0) ? 0 : (uint16_t)raw;
}

// A brief ST HIGH->LOW->HIGH pulse, timed to CLK edges. Called once to mark
// the start of integration and again to mark the end - the sequence is
// identical both times (see ACQUISITION PROTOCOL note up top). The single
// CLK falling edge that happens while ST is LOW is what actually triggers
// the sensor's internal shift register, per the datasheet.
void pulseStartStop() {
  digitalWrite(SPEC_CLK, LOW);
  delayMicroseconds(CLK_DELAY_US);
  digitalWrite(SPEC_CLK, HIGH);
  digitalWrite(SPEC_ST, LOW);
  delayMicroseconds(CLK_DELAY_US);

  digitalWrite(SPEC_CLK, LOW);
  delayMicroseconds(CLK_DELAY_US);
  digitalWrite(SPEC_CLK, HIGH);
  digitalWrite(SPEC_ST, HIGH);
  delayMicroseconds(CLK_DELAY_US);
}

// Run the sensor once and update the running average stored in g_data.
// Instead of summing into a wide accumulator, this function updates the
// average in-place using: new_avg = (prev_avg * prev_count + sample) /
// new_count. This avoids a large uint32_t accumulator array and keeps RAM
// usage low on AVR Uno boards. passIndex is 0 for the first pass, 1 for the
// second, etc.
void acquireOnePass(uint8_t passIndex) {
  // -- Step 1: one full frame's worth of leading clocks, ST idling HIGH. --
  for (int i = 0; i < SPEC_CHANNELS; i++) pulseClock();

  // -- Step 2: start pulse - marks the beginning of integration. --
  pulseStartStop();

  // -- Step 3: integration window. ST stays HIGH; CLK keeps running so the
  //    sensor's internal clocking stays alive, in groups of 2 pulseClock()
  //    calls (~1 clock cycle each). We time this against a real elapsed-
  //    microsecond count via micros(), NOT by estimating clock counts -
  //    digitalWrite() call overhead on the Uno is several microseconds per
  //    call, which dwarfs CLK_DELAY_US and made a clock-counting estimate
  //    badly inaccurate. This loop instead keeps pulsing until the
  //    requested integration_us has actually elapsed. --
  unsigned long intStart = micros();
  while ((unsigned long)(micros() - intStart) < g_cfg.integration_us) {
    pulseClock();
    pulseClock();
  }

  // -- Step 4: stop pulse - marks the end of integration; pixel shift-out
  //    begins right after. --
  pulseStartStop();

  // -- Step 5: read the 256 real pixels. Each pixel gets a 4-clock group;
  //    VIDEO is valid on the first clock's falling edge, so that's where we
  //    sample. The remaining 3 clocks just shift to the next pixel. --
  for (int i = 0; i < SPEC_CHANNELS; i++) {
    digitalWrite(SPEC_CLK, LOW);
    delayMicroseconds(CLK_DELAY_US);
    digitalWrite(SPEC_CLK, HIGH);
    delayMicroseconds(CLK_DELAY_US);
    digitalWrite(SPEC_CLK, LOW);
    // Settling delay before sampling. If a buffer op-amp is in the signal
    // path, this needs to cover its worst-case slew time, not just RC
    // settling: e.g. the OPA347's slew rate is only ~0.17 V/us typical, so
    // a full-scale step (a few volts, as can happen pixel-to-pixel at a
    // sharp spectral edge) can take ~15-20us to fully settle. Without a
    // buffer, a much shorter delay (a few us) is enough for the sensor's
    // own ~150 ohm output impedance to settle. Tune this to your actual
    // signal path; too short truncates real amplitude (readings looking
    // "compressed" versus the scope), too long just slows readout.
    delayMicroseconds(20);

    
    uint16_t sample = readMCP3301Unsigned();   // valid right after this falling edge

    if (passIndex == 0) {
      // first pass: initialize the running average
      g_data[i] = sample;
    } else {
      // update running average: (prev_avg * prev_count + sample) / new_count
      uint32_t accum = (uint32_t)g_data[i] * (uint32_t)passIndex + (uint32_t)sample;
      g_data[i] = (uint16_t)(accum / (uint32_t)(passIndex + 1));
    }

    digitalWrite(SPEC_CLK, HIGH);
    delayMicroseconds(CLK_DELAY_US);
    pulseClock();
    pulseClock();
  }

  // -- Step 6: one full frame's worth of trailing clocks, leaves the sensor
  //    ready for the next acquisition. --
  for (int i = 0; i < SPEC_CHANNELS; i++) pulseClock();
}

// Read a full spectrum into g_data, averaging g_cfg.n_avg passes using
// an in-place running average (so no wide accumulators are required).
void readSpectrometer() {
  // Initialize g_data to zero only to detect first pass; acquireOnePass(0)
  // treats passIndex==0 as an initializer.
  for (int i = 0; i < SPEC_CHANNELS; i++) g_data[i] = 0;

  for (uint8_t p = 0; p < g_cfg.n_avg; p++) acquireOnePass(p);

  // No final division required—the running average is already applied.
}

// Capture the current reading as the dark reference.
void captureDark() {
  readSpectrometer();
  for (int i = 0; i < SPEC_CHANNELS; i++) g_dark[i] = g_data[i];
  g_have_dark = true;
}

// ============================================================================
//  6. OUTPUT HELPERS
// ============================================================================

// Print the spectrum as one line of comma-separated numbers, ending in '\r'.
// raw == true  -> raw counts
// raw == false -> dark-subtracted counts (clamped at 0), if a dark frame exists
void printSpectrum(bool raw) {
  for (int i = 0; i < SPEC_CHANNELS; i++) {
    int value;
    if (raw || !g_have_dark) {
      value = g_data[i];
    } else {
      value = (int)g_data[i] - (int)g_dark[i];
      if (value < 0) value = 0;
    }
    Serial.print(value);
    if (i < SPEC_CHANNELS - 1) Serial.print(',');
  }
  Serial.print('\r');
}

// Print the six wavelength coefficients as a JSON array.
void printWlCoeffs() {
  Serial.print(F("{\"wl_coeffs\":["));
  for (int i = 0; i < 6; i++) {
    Serial.print(g_cfg.wl_coeffs[i], 8);   // 8 decimal places
    if (i < 5) Serial.print(',');
  }
  Serial.print(F("]}\r"));
}

// ============================================================================
//  7. COMMAND HANDLING
// ============================================================================
//
//  Supported commands (each line ends with Carriage Return, '\r'):
//    hello                     -> "C12666MA,v1.0"        (auto-discovery)
//    idn                       -> "C12666MA_UNO_v1.0"
//    spec                      -> dark-subtracted spectrum (256 numbers)
//    spec,raw                  -> raw spectrum (256 numbers)
//    set_integration,<us>      -> {"integration_us":<n>}  (floored at 1280)
//    get_integration           -> <n>
//    set_gain,<0|1>            -> {"high_gain":<0|1>}     (0=low, 1=high)
//    get_gain                  -> <0|1>
//    dark                      -> {"dark":"ok"}          (capture dark frame)
//    clear_dark                -> {"dark":"cleared"}
//    set_avg,<n>               -> {"n_avg":<n>}          (1..63)
//    get_avg                   -> <n>
//    set_wl_coeff,<i>,<value>  -> {"wl_coeffs":[...]}    (i = 0..5)
//    get_wl_coeffs             -> {"wl_coeffs":[...]}
//    set_name,<text>           -> {"device_name":"..."}
//    get_name                  -> <name>
//    reboot                    -> soft reset
//
void handleCommand(char *cmd) {
  // strtok() splits the command on commas. The first token is the command name.
  char *tok = strtok(cmd, ",");
  if (tok == NULL) return;

  if (strcmp(tok, "hello") == 0) {
    Serial.print(F("C12666MA,v1.0\r\n"));

  } else if (strcmp(tok, "idn") == 0) {
    Serial.print(F("C12666MA_UNO_v1.0\r\n"));

  } else if (strcmp(tok, "spec") == 0) {
    char *arg = strtok(NULL, ",");                 // optional "raw"
    bool raw = (arg != NULL && strcmp(arg, "raw") == 0);
    readSpectrometer();
    printSpectrum(raw);
    Serial.print("\r\n");

  } else if (strcmp(tok, "set_integration") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      unsigned long requested = atol(arg);
      g_cfg.integration_us = (requested < MIN_INTEGRATION_US) ? MIN_INTEGRATION_US : requested;
      saveConfig();
    }
    Serial.print(F("{\"integration_us\":"));
    Serial.print(g_cfg.integration_us);
    Serial.print(F("}\r\n"));

  } else if (strcmp(tok, "get_integration") == 0) {
    Serial.print(g_cfg.integration_us);
    Serial.print('\r');
    Serial.print("\n");

  } else if (strcmp(tok, "set_gain") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      g_cfg.high_gain = (atoi(arg) != 0) ? 1 : 0;
      applyGain();
      saveConfig();
    }
    Serial.print(F("{\"high_gain\":"));
    Serial.print(g_cfg.high_gain);
    Serial.print(F("}\r\n"));

  } else if (strcmp(tok, "get_gain") == 0) {
    Serial.print(g_cfg.high_gain);
    Serial.print(F("\r\n"));

  } else if (strcmp(tok, "dark") == 0) {
    captureDark();
    Serial.print(F("{\"dark\":\"ok\"}\r\n"));

  } else if (strcmp(tok, "clear_dark") == 0) {
    g_have_dark = false;
    Serial.print(F("{\"dark\":\"cleared\"}\r\n"));

  } else if (strcmp(tok, "set_avg") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      int n = atoi(arg);
      if (n < 1) n = 1;
      if (n > MAX_AVG) n = MAX_AVG;
      g_cfg.n_avg = (uint8_t)n;
      saveConfig();
    }
    Serial.print(F("{\"n_avg\":"));
    Serial.print(g_cfg.n_avg);
    Serial.print(F("}\r\n"));

  } else if (strcmp(tok, "get_avg") == 0) {
    Serial.print(g_cfg.n_avg);
    Serial.print(F("\r\n"));

  } else if (strcmp(tok, "set_wl_coeff") == 0) {
    char *sIndex = strtok(NULL, ",");
    char *sValue = strtok(NULL, ",");
    if (sIndex != NULL && sValue != NULL) {
      int idx = atoi(sIndex);
      if (idx >= 0 && idx < 6) {
        g_cfg.wl_coeffs[idx] = atof(sValue);       // text -> float
        saveConfig();
      }
    }
    printWlCoeffs();

  } else if (strcmp(tok, "get_wl_coeffs") == 0) {
    printWlCoeffs();

  } else if (strcmp(tok, "set_name") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      strncpy(g_cfg.name, arg, sizeof(g_cfg.name) - 1);
      g_cfg.name[sizeof(g_cfg.name) - 1] = '\0';   // always null-terminate
      saveConfig();
    }
    Serial.print(F("{\"device_name\":\""));
    Serial.print(g_cfg.name);
    Serial.print(F("\"}\r\n"));

  } else if (strcmp(tok, "get_name") == 0) {
    Serial.print(g_cfg.name);
    Serial.print('\r\n');

  } else if (strcmp(tok, "reboot") == 0) {
    Serial.print(F("{\"reboot\":\"ok\"}\r"));
    delay(50);            // let the reply flush out
    resetBoard();

  } else {
    Serial.print(F("{\"error\":\"unknown\"}\r\n"));
  }
}

// ============================================================================
//  8. setup() / loop()
// ============================================================================

void setup() {
  pinMode(SPEC_CLK,  OUTPUT);
  pinMode(SPEC_ST,   OUTPUT);
  pinMode(SPEC_GAIN, OUTPUT);
  digitalWrite(SPEC_CLK, LOW);
  digitalWrite(SPEC_ST,  HIGH);   // ST idles HIGH on the C12666MA, not LOW

  // MCP3301 chip-select: idle HIGH (deselected). CLK/MISO/MOSI (13/12/11)
  // are configured automatically by SPI.begin().
  // Pin 10 is the ATmega328's fixed hardware SS pin - even though CS is on
  // pin 9 instead, pin 10 must still be forced to OUTPUT or the SPI
  // hardware can silently drop out of Master mode (see MCP3301_bench_test.ino
  // for the full explanation).
  pinMode(10, OUTPUT);
  pinMode(ADC_CS, OUTPUT);
  digitalWrite(ADC_CS, HIGH);
  SPI.begin();
  // 1 MHz is comfortably within the MCP3301's supported clock range (up to
  // ~1.7-2 MHz typical at 5V per the datasheet) with plenty of margin;
  // increase later if you need faster full-frame readout. MODE0 (CPOL=0,
  // CPHA=0) matches the chip's supported SPI modes 0,0 and 1,1.
  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));

  Serial.begin(115200);
  loadConfig();
  applyGain();   // drive the Gain pin to match the loaded/default setting
}

void loop() {
  // Collect characters until we see a line terminator, then run the command.
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\r' || c == '\n') {
      if (g_cmd_len > 0) {
        g_cmd[g_cmd_len] = '\0';   // finish the string
        handleCommand(g_cmd);
        g_cmd_len = 0;
      }
    } else if (g_cmd_len < sizeof(g_cmd) - 1) {
      g_cmd[g_cmd_len++] = c;
    }
    // (characters beyond the buffer size are dropped on purpose)
  }
}
