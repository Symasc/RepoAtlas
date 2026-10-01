import type { NodeTypes } from "@xyflow/react";

import { nodeTypes as levelNodeTypes } from "./nodes";
import { traceNodeTypes } from "./traceNodes";

/**
 * Every node type the canvas draws. Module scope, so React Flow never sees a new
 * object and remounts every node; here rather than in either module, so neither
 * has to import the other at load.
 */
export const nodeTypes: NodeTypes = { ...levelNodeTypes, ...traceNodeTypes };
