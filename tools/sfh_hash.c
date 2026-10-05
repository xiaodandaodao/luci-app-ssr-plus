/*
 * sfh_hash —— 从 openwrt/luci 的 modules/luci-base/src/lib/lmo.c 摘出
 * （哈希算法原作者 Paul Hsieh，见 http://www.azillionmonkeys.com/qed/hash.html，
 *   lmo.c 整体为 Apache License 2.0）。
 *
 * 为什么要单独摘出来：
 *   官方 lmo.c 里这个函数与 "plural_formula.h" 同处一个翻译单元，而那个头文件
 *   是由 lemon 从 plural_formula.y 生成的（需要额外编 lemon）。po2lmo 其实只用
 *   得到 sfh_hash 一个函数，摘出来就能让构建 po2lmo 只需要「C 编译器 + lua 开发库」。
 */
#include <stddef.h>
#include <stdint.h>
#include "lmo.h"

uint32_t sfh_hash(const char *data, size_t len, uint32_t init)
{
	uint32_t hash = init, tmp;
	int rem;

	if (len <= 0 || data == NULL) return 0;

	rem = len & 3;
	len >>= 2;

	/* Main loop */
	for (;len > 0; len--) {
		hash  += sfh_get16(data);
		tmp    = (sfh_get16(data+2) << 11) ^ hash;
		hash   = (hash << 16) ^ tmp;
		data  += 2*sizeof(uint16_t);
		hash  += hash >> 11;
	}

	/* Handle end cases */
	switch (rem) {
		case 3: hash += sfh_get16(data);
			hash ^= hash << 16;
			hash ^= (signed char)data[sizeof(uint16_t)] << 18;
			hash += hash >> 11;
			break;
		case 2: hash += sfh_get16(data);
			hash ^= hash << 11;
			hash += hash >> 17;
			break;
		case 1: hash += (signed char)*data;
			hash ^= hash << 10;
			hash += hash >> 1;
	}

	/* Force "avalanching" of final 127 bits */
	hash ^= hash << 3;
	hash += hash >> 5;
	hash ^= hash << 4;
	hash += hash >> 17;
	hash ^= hash << 25;
	hash += hash >> 6;

	return hash;
}
