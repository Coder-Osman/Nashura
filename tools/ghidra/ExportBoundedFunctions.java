import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.google.gson.GsonBuilder;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.pcode.PcodeOpAST;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.SourceType;

public class ExportBoundedFunctions extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] arguments = getScriptArgs();
        if (arguments.length != 3) {
            throw new IllegalArgumentException("Expected SHA256, entries TSV, output directory");
        }
        if (!arguments[0].equals(currentProgram.getExecutableSHA256())) {
            throw new IllegalArgumentException("Imported binary fingerprint mismatch");
        }
        if (!currentProgram.getLanguageID().toString().startsWith("AARCH64:LE:64:")) {
            throw new IllegalArgumentException("This bounded decoder requires little-endian AArch64");
        }
        Path output = Path.of(arguments[2]);
        Files.createDirectories(output);
        DecompInterface decompiler = new DecompInterface();
        try {
            if (!decompiler.openProgram(currentProgram)) {
                throw new IllegalStateException(decompiler.getLastMessage());
            }
            for (String line : Files.readAllLines(Path.of(arguments[1]))) {
                monitor.checkCancelled();
                if (line.isBlank()) {
                    continue;
                }
                String[] fields = line.split("\t");
                if (fields.length != 3 || !fields[0].matches("[A-Za-z0-9_]+")) {
                    throw new IllegalArgumentException("Invalid bounded entry");
                }
                Address entry = toAddr(fields[1]);
                long size = Long.decode(fields[2]);
                if (size <= 0 || size > 1048576 || size % 4 != 0 || entry.getOffset() % 4 != 0) {
                    throw new IllegalArgumentException("Invalid AArch64 function extent");
                }
                AddressSet body = new AddressSet(entry, entry.add(size - 1));
                if (!currentProgram.getMemory().getLoadedAndInitializedAddressSet().contains(body)) {
                    throw new IllegalArgumentException("Extent is not file-backed initialized memory");
                }
                if (currentProgram.getFunctionManager().getFunctionsOverlapping(body).hasNext()) {
                    throw new IllegalStateException("Existing function overlaps " + fields[0]);
                }
                List<String> assembly = new ArrayList<>();
                List<Map<String, Object>> references = new ArrayList<>();
                for (Address address = entry; address.compareTo(body.getMaxAddress()) <= 0;
                        address = address.add(4)) {
                    Instruction instruction = currentProgram.getListing().getInstructionAt(address);
                    if (instruction == null) {
                        DisassembleCommand command = new DisassembleCommand(address, body, false);
                        if (!command.applyTo(currentProgram, monitor)) {
                            throw new IllegalStateException(command.getStatusMsg());
                        }
                        instruction = currentProgram.getListing().getInstructionAt(address);
                    }
                    if (instruction == null || instruction.getLength() != 4) {
                        throw new IllegalStateException("Undecoded instruction at " + address);
                    }
                    assembly.add("0x" + address + ": " + instruction);
                    for (Reference reference : instruction.getReferencesFrom()) {
                        references.add(Map.of("source", address.toString(),
                            "target", reference.getToAddress().toString(),
                            "type", reference.getReferenceType().toString()));
                    }
                }
                Function function = currentProgram.getFunctionManager().createFunction(
                    fields[0], entry, body, SourceType.USER_DEFINED);
                decompiler.flushCache();
                DecompileResults result = decompiler.decompileFunction(function, 60, monitor);
                Map<String, Object> dossier = new LinkedHashMap<>();
                dossier.put("source_sha256", arguments[0]);
                dossier.put("name", fields[0]);
                dossier.put("address", "0x" + entry);
                dossier.put("image_base", "0x" + currentProgram.getImageBase());
                dossier.put("body_bytes", size);
                dossier.put("analysis_scope", "explicit-bounds-no-global-auto-analysis");
                dossier.put("ok", result.decompileCompleted());
                dossier.put("diagnostic", result.getErrorMessage());
                dossier.put("pseudocode", result.decompileCompleted()
                    ? result.getDecompiledFunction().getC() : null);
                List<String> outside = new ArrayList<>();
                if (result.getHighFunction() != null) {
                    var operations = result.getHighFunction().getPcodeOps();
                    while (operations.hasNext()) {
                        PcodeOpAST operation = operations.next();
                        Address address = operation.getSeqnum().getTarget();
                        if (!body.contains(address)) {
                            outside.add("0x" + address);
                        }
                    }
                }
                dossier.put("high_pcode_outside_body", outside);
                dossier.put("assembly", assembly);
                dossier.put("outgoing_references", references);
                Files.writeString(output.resolve(fields[0] + ".json"),
                    new GsonBuilder().setPrettyPrinting().create().toJson(dossier) + "\n",
                    StandardCharsets.UTF_8, StandardOpenOption.CREATE_NEW);
                println(fields[0] + " decompiled=" + result.decompileCompleted()
                    + " instructions=" + assembly.size());
            }
        } finally {
            decompiler.dispose();
        }
    }
}
