import { useEffect, useRef ,useId} from "react";
import { Link } from "react-router-dom";

export default function ErrorFallback({
    error,
    resetErrorBoundary,
    message,
    takeFocus=false,
    escapeTo = "/dashboard",
    escapeLabel="Go to the dashboard"}){

    const headingId = useId();
    const headingRef = useRef(null);

    useEffect(()=>{
        if(takeFocus){
            headingRef.current?.focus();
        }
    }, [takeFocus]);

    return (
        <section
            className="card"
            role="alert"
            aria-labelledby={headingId}>
                <h2 id={headingId} ref={headingRef} tabIndex={-1}> {message ?? "Something went wrong"} </h2>
                <p>This part of the page could not be displayed. You can try again or reload the page.</p>
                <div className="row">
                    <button type="button" onClick={resetErrorBoundary}>
                        Try again
                    </button>
                    <button type="button" className="secondary" onClick={()=> window.location.reload()}>Reload the page</button>
                    <Link to={escapeTo}>{escapeLabel}</Link>
                </div>
            </section>
    )
}