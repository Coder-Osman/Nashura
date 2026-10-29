// Build against the Android 11 libpropertyinfoserializer sources, not a private format.
#include "property_info_serializer/property_info_serializer.h"

#include <fstream>
#include <iostream>

int main(int argc, char **argv) {
    if (argc != 2) return 64;
    std::string data, error;
    if (!android::properties::BuildTrie({}, "u:object_r:default_prop:s0", "string", &data, &error)) {
        std::cerr << error << '\n';
        return 1;
    }
    std::ofstream output(argv[1], std::ios::binary);
    output.write(data.data(), data.size());
    return output ? 0 : 2;
}
