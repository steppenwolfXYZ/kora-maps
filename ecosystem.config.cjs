module.exports = {
	apps: [
		{
			name: 'koramaps',
			script: 'build/index.js',
			cwd: __dirname,
			// Runtime config (PORT, ORIGIN, ...) comes from the .env file
			// deployed alongside the build (ENV_VARS_PROD secret).
			node_args: '--env-file=.env'
		}
	]
};
