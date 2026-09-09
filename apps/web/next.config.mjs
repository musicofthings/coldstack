/** @type {import('next').NextConfig} */
export default {
  env: { NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000" },
};
