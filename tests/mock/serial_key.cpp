// Stand-in for the company DLL: same ZLGKey interface, reads ECUserialnumber.txt from CWD.
#include <cstdio>
#include <cstring>
#include <cstdlib>
extern "C" int ZLGKey(const unsigned char* seed, unsigned short n, unsigned int level,
                      const char* variant, unsigned char* key, unsigned short* klen) {
    FILE* f = fopen("ECUserialnumber.txt", "rb");      // binary: a stray '\r' would break us
    if (!f) return -2;
    char l1[128] = {0}, l2[32] = {0}, l3[32] = {0};
    if (!fgets(l1, sizeof l1, f) || !fgets(l2, sizeof l2, f) || !fgets(l3, sizeof l3, f)) { fclose(f); return -3; }
    fclose(f);
    l1[strcspn(l1, "\n")] = 0;
    int a = atoi(l2), b = atoi(l3), sl = (int)strlen(l1);
    if (sl == 0 || *klen < n) return -4;
    for (int i = 0; i < n; ++i)
        key[i] = seed[i] ^ (unsigned char)l1[i % sl] ^ (unsigned char)(a * 16 + b) ^ (unsigned char)level;
    *klen = n;
    return 0;
}
