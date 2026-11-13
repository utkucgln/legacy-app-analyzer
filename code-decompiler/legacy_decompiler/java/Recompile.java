import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Recompiles every decompiled source file independently (one in-process javac call per file)
 * against the original classes and libraries, recording success and the first error.
 * Args: outDir classpath sourceLevel limit tool...
 */
public class Recompile {
    public static void main(String[] args) throws IOException {
        Path out = Paths.get(args[0]);
        String cp = args[1];
        String level = args[2];
        int limit = Integer.parseInt(args[3]);
        JavaCompiler javac = ToolProvider.getSystemJavaCompiler();
        Files.createDirectories(out.resolve("logs"));
        for (int i = 4; i < args.length; i++) {
            String tool = args[i];
            Path src = out.resolve(tool);
            if (!Files.isDirectory(src)) {
                continue;
            }
            List<Path> files;
            try (Stream<Path> walk = Files.walk(src)) {
                files = walk.filter(p -> p.toString().endsWith(".java")).sorted().collect(Collectors.toList());
            }
            if (limit > 0 && files.size() > limit) {
                files = new ArrayList<>(files);
                Collections.shuffle(files, new Random(42));
                files = files.subList(0, limit);
            }
            Path dest = out.resolve("recompiled").resolve(tool);
            Files.createDirectories(dest);
            try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(out.resolve("logs").resolve("recompile-" + tool + ".tsv")))) {
                for (Path f : files) {
                    ByteArrayOutputStream err = new ByteArrayOutputStream();
                    int rc = javac.run(null, null, err, "-nowarn", "-encoding", "UTF-8", "-source", level, "-target", level,
                            "-proc:none", "-cp", cp, "-d", dest.toString(), f.toString());
                    String first = "";
                    for (String line : err.toString("UTF-8").split("\n")) {
                        int at = line.indexOf("error:");
                        if (at >= 0) {
                            first = line.substring(at + 6).trim();
                            break;
                        }
                    }
                    w.println(src.relativize(f).toString().replace('\\', '/') + "\t" + (rc == 0 ? "OK" : "FAIL") + "\t" + first.replace('\t', ' '));
                }
            }
        }
    }
}
