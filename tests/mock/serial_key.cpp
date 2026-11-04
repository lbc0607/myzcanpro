// Stand-in for the company DLL: same ZLGKey interface, reads EcuSerialNum.txt from CWD.
#include <cstdio>
#include <cstring>
#include <cstdlib>
extern "C" int ZLGKey(const unsigned char* seed, unsigned short n, unsigned int level,
                      const char* variant, unsigned char* key, unsigned short* klen) {
    FILE* f = fopen("EcuSerialNum.txt", "rb");      // binary: a stray '\r' would break us
    if (!f) return -2;
    char l1[128] = {0}, l2[32] = {0}, l3[32] = {0};
    if (!fgets(l1, sizeof l1, f) || !fgets(l2, sizeof l2, f) || !fgets(l3, sizeof l3, f)) { fclose(f); return -3; }
    fclose(f);
    l1[strcspn(l1, "\n")] = 0;
    int a = atoi(l2), b = atoi(l3), sl = (int)strlen(l1);
    int num = (n == 2) ? a : b;                       // 2-byte seed -> line 2, otherwise (16-byte) line 3
    if (*klen < n) return -4;
    for (int i = 0; i < n; ++i)
        key[i] = seed[i] ^ (unsigned char)(sl ? l1[i % sl] : 0) ^ (unsigned char)(num + 1) ^ (unsigned char)level;
    *klen = n;
    return 0;
}
