import { describe, it, expect } from 'vitest';
import { ApiError } from "./api.js";

describe('ApiError', () => {
    it('sets the fields from options', () => {
        const e = new ApiError("Not found", {status: 404});
        expect(e.message).toBe("Not found");
        expect(e.name).toBe("ApiError");
        expect(e.status).toBe(404);
        expect(e instanceof Error).toBe(true);
    });

    it('works with no options', () => {
        const e = new ApiError("Not found");
        expect(e.status).toBe(undefined);
    });

    it('works with try catch', () => {
        try{
            throw new ApiError("Not found", {status:401});
        }catch(error){
            expect(error.status).toBe(401);
        }
    });
  
})
