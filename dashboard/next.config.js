const { apiTarget } = require('./api-target');

// Checked when the config loads, so a Vercel build without a usable API_URL stops right away
const apiUrl = apiTarget(process.env);

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Docker runs the small standalone server; Vercel packages the app its own way
  ...(process.env.VERCEL ? {} : { output: 'standalone' }),
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${apiUrl}/api/:path*`,
      },
      {
        source: '/v1/:path*',
        destination: `${apiUrl}/v1/:path*`,
      },
      {
        source: '/backend/:path*',
        destination: `${apiUrl}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
