#include <jni.h>
#include <stdlib.h>
__attribute__((constructor)) static void init(void){setenv("NASHURA_NATIVE_BRIDGE_OBSERVATION","1",1);}
JNIEXPORT void JNICALL Java_com_nashura_benchmark_BenchRunner_configure(JNIEnv *e,jclass c,jstring k,jstring v){
 (void)c;const char *key=(*e)->GetStringUTFChars(e,k,0);const char *value=(*e)->GetStringUTFChars(e,v,0);
 if(key&&value)setenv(key,value,1);
 if(value)(*e)->ReleaseStringUTFChars(e,v,value);if(key)(*e)->ReleaseStringUTFChars(e,k,key);
}
