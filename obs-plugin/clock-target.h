#ifndef CELESTE_CLOCK_TARGET_H
#define CELESTE_CLOCK_TARGET_H

#include <stdbool.h>

static bool parse_clock_port(const char *text, int *port)
{
	if (!text) {
		*port = 47776;
		return true;
	}
	if (!*text)
		return false;
	unsigned value = 0;
	for (const char *p = text; *p; ++p) {
		if (*p < '0' || *p > '9')
			return false;
		value = value * 10 + (unsigned)(*p - '0');
		if (value > 65535)
			return false;
	}
	if (value < 1024)
		return false;
	*port = (int)value;
	return true;
}

#endif
