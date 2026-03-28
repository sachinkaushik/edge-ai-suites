module.exports = {
    flowFile: 'flows.json',
    flowFilePretty: true,
    uiPort: process.env.PORT || 1880,
    diagnostics: {
        enabled: true,
        ui: true
    },
    logging: {
        console: {
            level: "info",
            metrics: false,
            audit: false
        }
    },
    editorTheme: {
        page: {
            title: "Retail Store Dashboard"
        },
        header: {
            title: "Retail Store Dashboard"
        },
        palette: {
            editable: true
        },
        projects: {
            enabled: false
        }
    },
    functionGlobalContext: {}
};
