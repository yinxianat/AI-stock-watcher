export function logRenderError(error,info){
    console.error("Render Error", error);
    console.error("Component Stack:", info.componentStack);
}