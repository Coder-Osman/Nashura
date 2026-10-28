'use strict';

function clean(value) { return ptr(value).and(ptr('0x00ffffffffffffff')); }

function readable(address, size) {
  const begin = clean(address);
  const range = Process.findRangeByAddress(begin);
  if (!range || range.protection.indexOf('r') < 0 || begin.add(size).compare(range.base.add(range.size)) > 0)
    throw new Error('unreadable table memory');
  return begin;
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes)).map(value => value.toString(16).padStart(2, '0')).join('');
}

rpc.exports = {
  snapshot: function (context, expectedGuard) {
    const module = Process.getModuleByName('libg.so');
    if (Process.arch !== 'arm64' || hex(module.base.add(0xdccb88).readByteArray(16)) !== expectedGuard)
      throw new Error('engine entry guard mismatch');
    const root = readable(context, 219 * 8);
    const tables = [];
    let total = 0;
    for (let index = 0; index < 219; index++) {
      let object = root.add(index * 8).readPointer();
      if (object.isNull()) continue;
      object = readable(object, 0x90);
      const capacity = object.add(0x38).readS32(), count = object.add(0x3c).readS32();
      const type = object.add(0x84).readS32(), owner = clean(object.add(0x78).readPointer());
      if (type !== index || !owner.equals(root) || count < 0 || count > 1000000 ||
          capacity < count || capacity > 1000000) throw new Error('table invariant failed at slot ' + index);
      let nonnull = 0;
      if (count) {
        const rows = readable(object.add(0x30).readPointer(), count * 8);
        for (let row = 0; row < count; row++) if (!rows.add(row * 8).readPointer().isNull()) nonnull++;
      }
      let csvRows = -1, csvColumns = -1;
      const csv = object.add(0x88).readPointer();
      if (!csv.isNull()) {
        const p = readable(csv, 0x58);
        csvRows = p.add(0x54).readS32(); csvColumns = p.add(0x34).readS32();
        if (csvRows < 0 || csvRows > 1000000 || csvColumns < 0 || csvColumns > 100000)
          throw new Error('csv invariant failed at slot ' + index);
      }
      if (nonnull !== count) throw new Error('null row at slot ' + index);
      tables.push([index, count, capacity, nonnull, csvRows, csvColumns]);
      total += count;
    }
    return {schema: 'nashura.android-catalog-layout.v1', tables: tables, totalRows: total,
      coverage: 'row counts and csv dimensions, not cell contents', catalogReadyPass: false};
  }
};
