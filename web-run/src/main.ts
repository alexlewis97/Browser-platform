import "./styles.css";
import { mount } from "./app";

const el = document.getElementById("app");
if (!el) throw new Error("Missing #app");
mount(el);
