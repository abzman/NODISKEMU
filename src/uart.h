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


   uart.h: Definitions for the UART access routines

*/

#ifndef UART_H
#define UART_H

#ifdef CONFIG_UART_DEBUG

#include "progmem.h"

void uart_init(void);
unsigned char uart_getc(void);
void uart_putc(char c);
void uart_puthex(uint8_t num);
void uart_trace(void *ptr, uint16_t start, uint16_t len);
void uart_flush(void);
void uart_puts_P(const char *text);
void uart_putcrlf(void);

#ifdef __AVR__
#  define printf(str,...) printf_P(PSTR(str), ##__VA_ARGS__)
#endif

#else

#define uart_init()    do {} while(0)
#define uart_getc()    0
#define uart_putc(x)   do {} while(0)
#define uart_puthex(x) do {} while(0)
#define uart_flush()   do {} while(0)
#define uart_puts_P(x) do {} while(0)
#define uart_putcrlf() do {} while(0)
#define uart_trace(a,b,c) do {} while(0)

/* Debug printf() calls must compile to nothing when there is no UART.
 *
 * Without this, a debug printf() ends up in avr-libc's printf() while
 * stdout is still NULL. vfprintf() then uses address 0 as a FILE struct,
 * which on AVR is the CPU register file: it clears r6/r7, tests a bit
 * of r3 and - depending on whatever value r3 happened to have after
 * power-on or after the bootloader - calls a "put" function pointer
 * taken from r8:r9. That's a jump to a random address.
 *
 * This caused the petSD+ start-up hang (since 2017-11-22, when more
 * printf()s were added to read_configuration()) and the
 * "SAVE creates an empty file and the device resets" bug, because
 * ieee.c calls printf() on every OPEN/LISTEN/DATA/CLOSE.
 *
 * stdio.h is included first so that its prototype for printf() is
 * not mangled by the macro below.
 */
#ifdef __AVR__
#  include <stdio.h>
#  define printf(...) do {} while(0)
#endif

#endif

#endif
