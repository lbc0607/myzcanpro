#ifndef _DLL_H_
#define _DLL_H_

/*
extern "C" int GenerateKeyEx(
    const unsigned char* iSeedArray, 
    unsigned short iSeedArraySize,
    const unsigned int iSecurityLevel, 
    const char* iVariant,
    unsigned char* ioKeyArray,
    unsigned int iKeyArraySize,
    unsigned int& oSize
);
*/
extern "C" int ZLGKey(
    const unsigned char*    iSeedArray,        // seed数组
    unsigned short          iSeedArraySize,    // seed数组大小
    unsigned int            iSecurityLevel,    // 安全级别 1,3,5...
    const char*             iVariant,          // 其他数据, 可设置为空
    unsigned char*          iKeyArray,         // key数组, 空间由用户构造
    unsigned short*         iKeyArraySize      // key数组大小, 函数返回时为key数组实际大小
);

#endif
