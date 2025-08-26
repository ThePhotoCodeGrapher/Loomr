const { writeFileSync } = require('fs');
// Create a simple CommonJS re-export for Node CJS users
writeFileSync('dist/index.cjs', "module.exports = require('./index.js');\n");
