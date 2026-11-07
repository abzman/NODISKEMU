/* NODISKEMU - SD/MMC to IEEE-488 interface/controller
   Copyright (C) 2007-2018  Ingo Korb <ingo@akana.de>

   NODISKEMU is a fork of sd2iec by Ingo Korb (et al.), http://sd2iec.de

   Inspired by MMC2IEC by Lars Pontoppidan et al.

   FAT filesystem access based on code from ChaN and Jim Brain, see ff.c|h.

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation; version 2 of the License only.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program; if not, write to the Free Software
   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA


   softi2c.c: Software I2C bus master

*/

#include <avr/io.h>
#include <util/delay.h>
#include "config.h"
#include "uart.h"
#include "i2c.h"

#define SOFTI2C_SDA _BV(SOFTI2C_BIT_SDA)
#define SOFTI2C_SCL _BV(SOFTI2C_BIT_SCL)

/* Upper bound for clock stretching, in loop iterations of ~1us each.  */
/* The previous code waited forever, so a single slave holding SCL low  */
/* (e.g. a confused I2C slave after a reset) froze the whole firmware.  */
#define SOFTI2C_STRETCH_LIMIT 5000

/* Set when a slave stretched the clock for too long during a transfer */
static uint8_t i2c_bus_error;

/* Cleared if the bus is unusable (see i2c_init); idea from upstream     */
/* sd2iec commit 90f3cbe "Disable i2c if bus in invalid state".           */
static uint8_t i2c_ready;

static void set_scl(uint8_t x) {
  if (x) {
    uint16_t limit = SOFTI2C_STRETCH_LIMIT;

    SOFTI2C_DDR  &= (uint8_t)~SOFTI2C_SCL;
    SOFTI2C_PORT |= SOFTI2C_SCL;
    // Clock stretching, with timeout
    while (!(SOFTI2C_PIN & SOFTI2C_SCL)) {
      if (--limit == 0) {
        i2c_bus_error = 1;
        break;
      }
      _delay_us(1);
    }
  } else {
    SOFTI2C_DDR  |= SOFTI2C_SCL;
    SOFTI2C_PORT &= (uint8_t)~SOFTI2C_SCL;
  }
}

static void set_sda(uint8_t x) {
  if (x) {
    SOFTI2C_DDR  &= (uint8_t)~SOFTI2C_SDA;
    SOFTI2C_PORT |= SOFTI2C_SDA;
  } else {
    SOFTI2C_DDR  |= SOFTI2C_SDA;
    SOFTI2C_PORT &= (uint8_t)~SOFTI2C_SDA;
  }
}


static void start_condition(void) {
  set_sda(1);
  set_scl(1);
  _delay_us(SOFTI2C_DELAY);
  set_sda(0);
  _delay_us(SOFTI2C_DELAY);
  set_scl(0);
}

static void stop_condition(void) {
  set_sda(0);
  _delay_us(SOFTI2C_DELAY);
  set_scl(1);
  _delay_us(SOFTI2C_DELAY);
  set_sda(1);
  _delay_us(SOFTI2C_DELAY);
}

/* Returns value of the acknowledge bit */
static uint8_t i2c_send_byte(uint8_t value) {
  uint8_t i;

  for (i=8;i!=0;i--) {
    set_scl(0);
    _delay_us(SOFTI2C_DELAY/2);
    set_sda(value & 128);
    _delay_us(SOFTI2C_DELAY/2);
    set_scl(1);
    _delay_us(SOFTI2C_DELAY);
    value <<= 1;
  }
  set_scl(0);
  _delay_us(SOFTI2C_DELAY/2);
  set_sda(1);
  _delay_us(SOFTI2C_DELAY/2);
  set_scl(1);
  _delay_us(SOFTI2C_DELAY/2);
  i = !!(SOFTI2C_PIN & SOFTI2C_SDA);
  _delay_us(SOFTI2C_DELAY/2);
  set_scl(0);

  return i;
}

/* Returns value of the byte read */
static uint8_t i2c_recv_byte(uint8_t sendack) {
  uint8_t i;
  uint8_t value = 0;

  set_sda(1);
  _delay_us(SOFTI2C_DELAY/2);
  for (i=8;i!=0;i--) {
    _delay_us(SOFTI2C_DELAY/2);
    set_scl(1);
    _delay_us(SOFTI2C_DELAY/2);
    value = (value << 1) + !!(SOFTI2C_PIN & SOFTI2C_SDA);
    _delay_us(SOFTI2C_DELAY/2);
    set_scl(0);
    _delay_us(SOFTI2C_DELAY/2);
  }
  set_sda(!sendack);
  _delay_us(SOFTI2C_DELAY/2);
  set_scl(1);
  _delay_us(SOFTI2C_DELAY);
  set_scl(0);
  set_sda(1);

  return value;
}

/* Returns 1 if there was no ACK to the address */
uint8_t i2c_write_register(uint8_t address, uint8_t reg, uint8_t val) {

  if (!i2c_ready)
    return 1;
  i2c_bus_error = 0;
  start_condition();
  if (i2c_send_byte(address)) {
    stop_condition();
    return 1;
  }
  i2c_send_byte(reg);
  i2c_send_byte(val);
  stop_condition();
  return i2c_bus_error;
}

/* Returns 1 if there was no ACK to the address */
uint8_t i2c_write_registers(uint8_t address, uint8_t startreg, uint8_t count, const void *data) {
  uint8_t i;

  if (!i2c_ready)
    return 1;

  i2c_bus_error = 0;
  start_condition();
  if (i2c_send_byte(address)) {
    stop_condition();
    return 1;
  }
  i2c_send_byte(startreg);
  for (i=0;i<count;i++)
    i2c_send_byte(((uint8_t *)data)[i]);
  stop_condition();
  return i2c_bus_error;
}

/* Returns -1 if there was no ACK to the address, register contents otherwise */
int16_t i2c_read_register(uint8_t address, uint8_t reg) {
  uint8_t val;

  if (!i2c_ready)
    return -1;

  i2c_bus_error = 0;
  start_condition();
  if (i2c_send_byte(address)) {
    stop_condition();
    return -1;
  }
  i2c_send_byte(reg);
  start_condition();
  if (i2c_send_byte(address|1)) {
    stop_condition();
    return -1;
  }
  val = i2c_recv_byte(0);
  stop_condition();
  if (i2c_bus_error)
    return -1;
  return val;
}

/* Returns 1 if there was no ACK to the address */
uint8_t i2c_read_registers(uint8_t address, uint8_t startreg, uint8_t count, void *data) {
  uint8_t i;

  if (!i2c_ready)
    return 1;

  i2c_bus_error = 0;
  start_condition();
  if (i2c_send_byte(address)) {
    stop_condition();
    return 1;
  }
  i2c_send_byte(startreg);
  start_condition();
  if (i2c_send_byte(address|1)) {
    stop_condition();
    return 1;
  }
  for (i=0;i<count-1;i++)
    ((uint8_t *)data)[i] = i2c_recv_byte(1);
  ((uint8_t *)data)[i] = i2c_recv_byte(0);
  stop_condition();
  return i2c_bus_error;
}

void i2c_init(void) {
  /* Set I2C pins to input -> high with external pullups */
  SOFTI2C_DDR  &= (uint8_t)~(SOFTI2C_SCL|SOFTI2C_SDA);
  SOFTI2C_PORT &= (uint8_t)~(SOFTI2C_SCL|SOFTI2C_SDA);

  /* Release both lines with the internal pull-ups enabled (we can't  */
  /* rely on external pull-ups being fitted on every board revision).  */
  /* If SCL is still held low, nothing can be done: disable I2C.       */
  SOFTI2C_PORT |= SOFTI2C_SCL|SOFTI2C_SDA;
  _delay_us(20);
  if (!(SOFTI2C_PIN & SOFTI2C_SCL)) {
    uart_puts_P(PSTR("i2c disabled (SCL low)\r\n"));
    i2c_ready = 0;
    return;
  }

  set_sda(1);
  set_scl(1);

  /* Bus recovery. The battery-backed RTC and the PWM controller on   */
  /* petSD+ 2.x boards are not reset together with the AVR, so after  */
  /* a reset in the middle of a transfer a slave may still be driving */
  /* SDA low, waiting for more clocks. Clock it out (at most 9 bits), */
  /* then generate START+STOP so every slave is back to idle.         */
  for (uint8_t i = 0; i < 9 && !(SOFTI2C_PIN & SOFTI2C_SDA); i++) {
    set_scl(0);
    _delay_us(SOFTI2C_DELAY);
    set_scl(1);
    _delay_us(SOFTI2C_DELAY);
  }
  start_condition();
  stop_condition();

  /* Still not idle? Then the bus is broken: don't use it at all */
  if ((SOFTI2C_PIN & (SOFTI2C_SDA|SOFTI2C_SCL)) != (SOFTI2C_SDA|SOFTI2C_SCL)) {
    uart_puts_P(PSTR("i2c disabled (bus stuck)\r\n"));
    i2c_ready = 0;
    return;
  }

  i2c_bus_error = 0;
  i2c_ready = 1;
}
