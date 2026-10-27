#include "dll.h"

/*
*iSeedArray: Array for the seed [in]
*iSeedArraySize: Length of the array for the seed [in]
*iSecurityLevel: Security level [in]
*iVariant: Name of the active variant [in]
*ioKeyArray: Array for the key [in, out]
*iKeyArraySize: Maximum length of the array for the key [in]
*oSize: Length of the key [out]
*/
/*
extern "C" int GenerateKeyEx( const unsigned char* iSeedArray, unsigned short iSeedArraySize, const unsigned int iSecurityLevel
							 , const char* iVariant, unsigned char* ioKeyArray, unsigned int iKeyArraySize, unsigned int& oSize )
{
	if (0 == iSeedArray || 0 == ioKeyArray) return -1;
	for (unsigned short i = 0; i < iSeedArraySize; ++i)
	{
		ioKeyArray[i] = iSeedArray[i] + 1;
	}
	oSize = iSeedArraySize;
	return 0;
}
*/

extern "C" int ZLGKey( const unsigned char* iSeedArray
					  , unsigned short iSeedArraySize
					  , unsigned int iSecurityLevel
					  , const char* iVariant
					  , unsigned char* iKeyArray
					  , unsigned short* iKeyArraySize )
{
	// 以下代码仅为例子, 具体算法自定义实现
	if (0 == iSeedArray || 0 == iKeyArray) return -1;
	for (unsigned short i = 0; i < iSeedArraySize; ++i)
	{
		iKeyArray[i] = iSeedArray[i] + 1;
	}
	*iKeyArraySize = iSeedArraySize;
	return 0;
}