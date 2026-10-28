// Workaround for VS2022 14.42+ with UE5.1 engine headers
// __has_feature is a Clang built-in; MSVC doesn't recognize it in preprocessor
#ifndef __has_feature
#define __has_feature(x) 0
#endif
#pragma warning(push)
#pragma warning(disable: 4668)
#pragma warning(disable: 4067)
