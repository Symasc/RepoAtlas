// web: the TypeScript caller. One function per scenario.
import axios from "axios";

// A: route only.
export async function helloRelative() {
    return fetch("/hello");
}

// B: the endpoint api-a's specification declares.
export async function helloAbsolute() {
    return fetch("https://api.example.com/hello");
}

// C: Swagger 2 base path.
export async function listUsersV1() {
    return fetch("https://api.example.com/v1/users");
}

// D: same route as api-d, different deployment.
export async function pingOtherDeployment() {
    return fetch("https://api-b.example.com/ping");
}

// E: api-a declares POST /users; this is GET.
export async function listUsers() {
    return fetch("https://api.example.com/users");
}

// G: one of api-a's two deployments.
export async function helloEu() {
    return fetch("https://api-eu.example.com/hello");
}

// H: two declarers of one endpoint.
export async function health() {
    return fetch("https://api.example.com/health", { method: "GET" });
}

// I: a concrete path is not a parameter template.
export async function user42() {
    return fetch("/users/42");
}

export async function createUser(body: unknown) {
    return axios.post("https://api.example.com/users", body);
}
