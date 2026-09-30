// Run inside a Web Worker: elkjs's script sees a worker global (no `document`)
// and answers layout requests on `self.onmessage` by itself.
import "elkjs/lib/elk-worker.min.js";
